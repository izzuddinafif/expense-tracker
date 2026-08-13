package com.afif.expensetracker

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkManager
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.notification.BankNotificationParser
import com.afif.expensetracker.notification.BankNotificationSources
import com.afif.expensetracker.notification.NotificationIngestor
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.first
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class NotificationSyncE2eTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val database get() = LedgerDatabase.get(context)
    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        runBlocking {
            withContext(Dispatchers.IO) {
                WorkManager.getInstance(context).cancelUniqueWork("ledger-sync").result.get()
                database.clearAllTables()
            }
        }
        LedgerSettingsStore.clearForTests(context)
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun onlyExactStableNotificationIdentityIsDeduplicated() {
        val packageName = BankNotificationSources.BSI_BYOND_PACKAGE
        val title = "BYOND by BSI"
        val body = "Pembayaran Rp25.000 di TOKO MAJU pada 12/07/2026"
        val ingestor = NotificationIngestor(database.notificationDao()) { 1_721_065_200_000L }

        val first = runBlocking(Dispatchers.IO) {
            ingestor.ingest(packageName, title, body, sourceIdentity = "notification-key-first")
        }
        val duplicate = runBlocking(Dispatchers.IO) {
            ingestor.ingest(packageName, title, body, sourceIdentity = "notification-key-first")
        }
        val separatePurchase = runBlocking(Dispatchers.IO) {
            ingestor.ingest(packageName, title, body, sourceIdentity = "notification-key-second")
        }

        assertEquals(first, duplicate)
        org.junit.Assert.assertNotEquals(first, separatePurchase)
        val records = runBlocking(Dispatchers.IO) { database.notificationDao().observeRecent(20).first() }
        assertEquals(2, records.size)
        assertEquals(first, records.single { it.id == separatePurchase }.suspectedDuplicateOf)
        compose.onNodeWithTag("nav_inbox").performClick()
        compose.onNodeWithTag("suspected_repost_${records.single { it.id == separatePurchase }.sourceRef}")
            .assertIsDisplayed()
    }

    @Test
    fun bsiNotificationCanBeConfirmedAndReconciledWithCanonicalTransaction() {
        val packageName = BankNotificationSources.BSI_BYOND_PACKAGE
        val title = "BYOND by BSI"
        val body = "Pembayaran Rp25.000 di TOKO MAJU pada 12/07/2026"
        val parsedRef = BankNotificationParser.fingerprint(packageName, title, body)
        val sourceIdentity = "bsi-notification-key-1"
        val sourceRef = BankNotificationParser.fingerprint(
            packageName,
            parsedRef,
            sourceIdentity,
        )
        val localId = "android-$sourceRef"

        runBlocking {
            withContext(Dispatchers.IO) {
                NotificationIngestor(database.notificationDao()) { 1_721_065_200_000L }
                    .ingest(packageName, title, body, sourceIdentity)
            }
        }

        val canonicalJson = """
            {
              "id":"server-bsi-1",
              "kind":"expense",
              "amount_idr":25000,
              "occurred_on":"2026-07-12",
              "description":"BYOND by BSI",
              "merchant":"TOKO MAJU",
              "category":"Shopping",
              "subcategory":"Retail",
              "status":"confirmed"
            }
        """.trimIndent()
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"transaction":$canonicalJson}"""))
        server.enqueue(MockResponse().setResponseCode(200).setBody("""{"transactions":[$canonicalJson],"next_cursor":null}"""))
        context.getSharedPreferences("ledger_settings", 0).edit()
            .putString("api_base_url", server.url("/").toString())
            .putString("device_token", "instrumentation-token")
            .commit()

        compose.onNodeWithTag("nav_inbox").performClick()
        compose.onNodeWithTag("inbox_item_$sourceRef").assertIsDisplayed()
        compose.onNodeWithTag("confirm_$sourceRef").performClick()
        compose.waitUntil(timeoutMillis = 30_000) {
            runBlocking {
                withContext(Dispatchers.IO) {
                    database.notificationDao().findBySourceRef(sourceRef)?.status == "confirmed" &&
                        database.transactionDao().findById("server-bsi-1") != null &&
                        database.syncDao().pendingCount() == 0
                }
            }
        }

        runBlocking {
            withContext(Dispatchers.IO) {
                assertNull(database.transactionDao().findById(localId))
                val canonical = database.transactionDao().findById("server-bsi-1")
                assertNotNull(canonical)
                assertEquals(-25_000L, canonical?.amountMinor)
                assertEquals("TOKO MAJU", canonical?.merchant)
                assertEquals(0, database.syncDao().pendingCount())
            }
        }
        val pushRequest = server.takeRequest()
        val pullRequest = server.takeRequest()
        assertEquals("Bearer instrumentation-token", pushRequest.getHeader("Authorization"))
        assertEquals("/api/v1/transactions", pushRequest.path)
        assertEquals("/api/v1/transactions/changes?limit=200", pullRequest.path)
    }
}
