package com.afif.expensetracker

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.semantics.SemanticsActions
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkManager
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.manual.ManualTransactionDraft
import com.afif.expensetracker.manual.ManualTransactionKind
import com.afif.expensetracker.manual.ManualTransactionStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import java.time.LocalDate
import java.time.ZoneId

@RunWith(AndroidJUnit4::class)
class ManualTransactionUiTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    private val context
        get() = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val database
        get() = LedgerDatabase.get(context)

    @Before
    fun setUp() {
        runBlocking {
            withContext(Dispatchers.IO) {
                WorkManager.getInstance(context).cancelUniqueWork("ledger-sync").result.get()
                WorkManager.getInstance(context).cancelUniqueWork("ledger-sync-periodic").result.get()
                database.clearAllTables()
            }
        }
        LedgerSettingsStore.clearForTests(context)
    }

    @Test
    fun rapidManualIncomeSubmissionPersistsAndQueuesExactlyOnce() {
        val expectedDate = LocalDate.now()
        compose.onNodeWithTag("nav_transactions").performClick()
        compose.onNodeWithTag("transaction_add").performClick()
        compose.onNodeWithTag("manual_kind_income").performClick()

        fill("manual_description", "Salary")
        fill("manual_merchant", "Employer")
        fill("manual_amount", "7500000")
        compose.onNodeWithTag("manual_amount_preview", useUnmergedTree = true)
            .assertTextEquals("Rp7.500.000")
        fill("manual_category", "Salary")
        val accountSuggestion = compose.onNodeWithTag("manual_account_suggestion_1")
        accountSuggestion.performScrollTo()
        accountSuggestion.assertTextEquals("Mandiri")
        val accountSuggestionAction = accountSuggestion
            .fetchSemanticsNode()
            .config[SemanticsActions.OnClick]
            .action
        assertNotNull(accountSuggestionAction)
        compose.runOnUiThread { accountSuggestionAction?.invoke() }
        compose.waitForIdle()
        compose.onNodeWithTag("manual_account").assertTextContains("Mandiri")
        val saveAction = compose.onNodeWithTag("manual_save")
            .fetchSemanticsNode()
            .config[SemanticsActions.OnClick]
            .action
        assertNotNull(saveAction)
        compose.runOnUiThread {
            saveAction?.invoke()
            saveAction?.invoke()
        }

        var operationEntityId: String? = null
        compose.waitUntil(timeoutMillis = 10_000) {
            runBlocking(Dispatchers.IO) {
                val pending = database.syncDao().pending().filter { it.kind == "transaction" }
                pending.size == 1 && pending.single().entityId.also { operationEntityId = it }.isNotBlank()
            }
        }

        val entityId = operationEntityId
        assertNotNull(entityId)
        runBlocking(Dispatchers.IO) {
            val transaction = database.transactionDao().findById(entityId!!)
            assertNotNull(transaction)
            assertEquals(7_500_000L, transaction?.amountMinor)
            assertEquals("Salary", transaction?.description)
            assertEquals("Employer", transaction?.merchant)
            assertEquals("Salary", transaction?.category)
            assertEquals("Mandiri", transaction?.account)
            assertEquals(
                expectedDate
                    .atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli(),
                transaction?.occurredAt,
            )

            val operation = database.syncDao().findLatest("transaction", entityId)
            assertNotNull(operation)
            assertEquals("pending", operation?.state)
            val payload = JSONObject(operation?.payload.orEmpty())
            assertEquals("manual", payload.getString("source"))
            assertEquals("income", payload.getString("kind"))
            assertEquals(7_500_000L, payload.getLong("amount_idr"))
            assertEquals("Salary", payload.getString("subcategory"))
            assertEquals(entityId, payload.getString("source_ref"))
            assertTrue(payload.getBoolean("confirm"))
        }
    }

    @Test
    fun invalidManualExpenseShowsErrorAndDoesNotQueue() {
        compose.onNodeWithTag("nav_transactions").performClick()
        compose.onNodeWithTag("transaction_add").performClick()
        compose.onNodeWithTag("manual_date_picker_open")
            .performScrollTo()
            .performClick()
        compose.onNodeWithTag("manual_date_picker").fetchSemanticsNode()
        compose.onNodeWithTag("manual_date_picker_confirm").performClick()
        compose.onNodeWithTag("manual_date").assertTextContains(LocalDate.now().toString())
        compose.onNodeWithTag("manual_save").performClick()

        compose.onNodeWithTag("manual_error")
            .performScrollTo()
            .assertTextEquals("Add a description.")
        runBlocking(Dispatchers.IO) {
            assertTrue(database.syncDao().pending().isEmpty())
        }
    }

    @Test
    fun pendingManualEntryCanBeEditedThenVoidedWithoutServerSync() {
        val entityId = "android-manual-ui-pending"
        runBlocking(Dispatchers.IO) {
            ManualTransactionStore(
                database = database,
                idFactory = { entityId },
            ).create(
                ManualTransactionDraft(
                    kind = ManualTransactionKind.EXPENSE,
                    description = "Original lunch",
                    merchant = "Warung Lama",
                    amountIdr = 25_000,
                    occurredOn = LocalDate.now().toString(),
                    category = "Dining",
                    account = "Jago",
                ),
            )
        }

        compose.onNodeWithTag("nav_transactions").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_item_$entityId")
                .fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("transaction_item_$entityId").performClick()
        fill("transaction_description", "Corrected lunch")
        fill("transaction_amount", "27500")
        compose.onNodeWithTag("transaction_save").performScrollTo().performClick()

        compose.waitUntil(timeoutMillis = 10_000) {
            runBlocking(Dispatchers.IO) {
                val operation = database.syncDao().findLatest("transaction", entityId)
                val payload = runCatching { JSONObject(operation?.payload.orEmpty()) }.getOrNull()
                operation?.state == "pending" &&
                    payload?.optString("description") == "Corrected lunch" &&
                    payload.optLong("amount_idr") == 27_500L
            }
        }
        runBlocking(Dispatchers.IO) {
            val operations = database.syncDao().pending().filter { it.entityId == entityId }
            assertEquals(1, operations.size)
            assertEquals("transaction", operations.single().kind)
            assertEquals(-27_500L, database.transactionDao().findById(entityId)?.amountMinor)
        }

        compose.onNodeWithTag("transaction_void").performScrollTo().performClick()
        compose.onNodeWithTag("transaction_void_confirm").performClick()
        compose.waitUntil(timeoutMillis = 10_000) {
            runBlocking(Dispatchers.IO) {
                database.transactionDao().findById(entityId) == null &&
                    database.syncDao().findLatest("transaction", entityId) == null
            }
        }
        compose.onNodeWithTag("nav_transactions").assertTextContains("History")
    }

    @Test
    fun syncClaimPreventsPendingCreateCompactionRace() {
        val entityId = "android-manual-claim-race"
        runBlocking(Dispatchers.IO) {
            ManualTransactionStore(
                database = database,
                idFactory = { entityId },
            ).create(
                ManualTransactionDraft(
                    kind = ManualTransactionKind.EXPENSE,
                    description = "Claim test",
                    merchant = "Warung",
                    amountIdr = 10_000,
                    occurredOn = LocalDate.now().toString(),
                    category = "Dining",
                    account = "BSI",
                ),
            )
            val operation = database.syncDao().findLatest("transaction", entityId)
            assertNotNull(operation)

            assertEquals(1, database.syncDao().claimPending(operation!!.id, now = 1_000L))
            assertEquals(
                0,
                database.syncDao().replacePendingCreatePayload(
                    operation.id,
                    operation.payload,
                    now = 1_001L,
                ),
            )
            assertEquals(0, database.syncDao().discardPendingCreate(operation.id))
            assertEquals("sending", database.syncDao().findById(operation.id)?.state)

            assertEquals(1, database.syncDao().requeueExpiredClaims(before = 1_001L))
            assertEquals(
                1,
                database.syncDao().replacePendingCreatePayload(
                    operation.id,
                    operation.payload,
                    now = 1_002L,
                ),
            )
        }
    }

    private fun fill(tag: String, value: String) {
        val field = compose.onNodeWithTag(tag)
        field.performScrollTo()
        field.performTextClearance()
        field.performTextInput(value)
    }
}
