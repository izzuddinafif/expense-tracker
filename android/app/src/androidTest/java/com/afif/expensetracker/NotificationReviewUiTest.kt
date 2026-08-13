package com.afif.expensetracker

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkManager
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.data.NotificationRecord
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.time.LocalDate
import java.time.ZoneId

@RunWith(AndroidJUnit4::class)
class NotificationReviewUiTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    private val context get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val database get() = LedgerDatabase.get(context)

    @Before
    fun setUp() {
        runBlocking {
            withContext(Dispatchers.IO) {
                WorkManager.getInstance(context).cancelUniqueWork("ledger-sync").result.get()
                WorkManager.getInstance(context).cancelUniqueWork("ledger-sync-periodic").result.get()
                database.clearAllTables()
            }
        }
        // Keep the test local: the confirmation should remain in the outbox rather
        // than being consumed by a worker configured with a real API endpoint.
        LedgerSettingsStore.clearForTests(context)
    }

    @Test
    fun reviewRequiredNotificationCanBeCorrectedAndSavedToLocalOutbox() {
        val sourceRef = "review-ui-source-1"
        val receivedAt = 1_753_200_000_000L
        runBlocking(Dispatchers.IO) {
            database.notificationDao().enqueue(
                NotificationRecord(
                    sourceRef = sourceRef,
                    packageName = "id.co.banksyariah.bsi",
                    title = "BSI notification",
                    body = "Pembayaran terdeteksi",
                    amountIdr = 12_000L,
                    merchant = "Detected merchant",
                    bank = "BSI",
                    occurredOn = "2026-07-28",
                    reviewRequired = true,
                    receivedAt = receivedAt,
                ),
            )
        }

        compose.onNodeWithTag("nav_inbox").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("inbox_item_$sourceRef")
                .fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("inbox_item_$sourceRef").assertIsDisplayed()
        compose.onNodeWithTag("confirm_$sourceRef").performClick()
        compose.onNodeWithTag("review_merchant").assertIsDisplayed()

        replace("review_merchant", "Corrected merchant")
        replace("review_amount", "98765")
        replace("review_date", "2026-07-29")
        replace("review_description", "Corrected purchase description")
        replace("review_category", "Groceries")
        replace("review_account", "Jago")
        compose.onNodeWithTag("review_save").performClick()

        val transactionId = "android-$sourceRef"
        compose.waitUntil(timeoutMillis = 10_000) {
            runBlocking(Dispatchers.IO) {
                database.transactionDao().findById(transactionId) != null &&
                    database.notificationDao().findBySourceRef(sourceRef)?.status == "confirmed" &&
                    database.syncDao().findLatest("transaction", transactionId) != null
            }
        }

        runBlocking(Dispatchers.IO) {
            val transaction = database.transactionDao().findById(transactionId)
            assertNotNull(transaction)
            assertEquals("Corrected merchant", transaction?.merchant)
            assertEquals(-98_765L, transaction?.amountMinor)
            assertEquals("Corrected purchase description", transaction?.description)
            assertEquals("Groceries", transaction?.category)
            assertEquals("Jago", transaction?.account)
            assertEquals(
                LocalDate.parse("2026-07-29")
                    .atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli(),
                transaction?.occurredAt,
            )

            val operation = database.syncDao().findLatest("transaction", transactionId)
            assertNotNull(operation)
            assertEquals("pending", operation?.state)
            val payload = JSONObject(operation?.payload.orEmpty())
            assertEquals("expense", payload.getString("kind"))
            assertEquals(98_765L, payload.getLong("amount_idr"))
            assertEquals("2026-07-29", payload.getString("occurred_on"))
            assertEquals("Corrected purchase description", payload.getString("description"))
            assertEquals("Corrected merchant", payload.getString("merchant"))
            assertEquals("Groceries", payload.getString("category"))
            assertEquals("Jago", payload.getString("account"))
            assertEquals(sourceRef, payload.getString("source_ref"))
            assertEquals(true, payload.getBoolean("confirm"))
        }
    }

    private fun replace(tag: String, value: String) {
        compose.onNodeWithTag(tag).performScrollTo().performTextClearance()
        compose.onNodeWithTag(tag).performTextInput(value)
    }
}
