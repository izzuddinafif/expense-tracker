package com.afif.expensetracker

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.data.NotificationRecord
import com.afif.expensetracker.data.TransactionEntity
import com.afif.expensetracker.data.SyncOperation
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.runBlocking
import org.junit.Before
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class LedgerAppTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    @Before
    fun clearSettings() {
        LedgerSettingsStore.clearForTests(compose.activity)
        runBlocking(Dispatchers.IO) {
            LedgerDatabase.get(compose.activity).clearAllTables()
        }
    }

    @Test
    fun primaryNavigationShowsEveryOperationalScreen() {
        compose.onNodeWithText("Overview").assertIsDisplayed()

        compose.onNodeWithTag("nav_inbox").performClick()
        compose.onNodeWithText("Review inbox").assertIsDisplayed()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithText("Inbox is clear.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("Inbox is clear.").assertIsDisplayed()

        compose.onNodeWithTag("nav_transactions").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithText("No transactions yet.").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("No transactions yet.").assertIsDisplayed()

        compose.onNodeWithTag("nav_budgets").performClick()
        compose.onNodeWithText("Connect the ledger first").assertIsDisplayed()

        compose.onNodeWithTag("nav_settings").performClick()
        compose.waitForIdle()
        compose.onNodeWithText("Notification capture").assertExists()
    }

    @Test
    fun settingsPersistBackendConfiguration() {
        compose.onNodeWithTag("nav_settings").performClick()
        compose.waitForIdle()
        compose.onNodeWithTag("api_base_url", useUnmergedTree = true)
            .performTextInput("http://10.0.2.2:8080/")
        compose.onNodeWithTag("device_token", useUnmergedTree = true)
            .performTextInput("test-token-012345678901234567890123")
        compose.onNodeWithText("Save and sync").performClick()
        compose.onNodeWithTag("settings_message").assertIsDisplayed()

        val settings = LedgerSettingsStore.read(compose.activity)
        assertEquals("http://10.0.2.2:8080", settings.baseUrl)
        assertEquals("test-token-012345678901234567890123", settings.token)
        assertEquals(null, compose.activity.getSharedPreferences("ledger_settings", 0).getString("device_token", null))
    }

    @Test
    fun diagnosticsCanRetryOrDiscardFailedLocalOperations() {
        val db = LedgerDatabase.get(compose.activity)
        val retryId = runBlocking(Dispatchers.IO) {
            val operation = SyncOperation(kind = "transaction_update", entityId = "retry-row", payload = "{}", state = "failed", attempts = 5, lastError = "offline")
            db.syncDao().enqueue(operation)
            db.syncDao().failed().single().id
        }
        compose.onNodeWithTag("nav_settings").performClick()
        compose.onNodeWithTag("open_diagnostics").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("local_sync_retry_$retryId").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("local_sync_retry_$retryId").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            runBlocking(Dispatchers.IO) { db.syncDao().findById(retryId)?.state != "failed" }
        }

        val discardEntityId = "android-manual-discard-row"
        val discardId = runBlocking(Dispatchers.IO) {
            db.transactionDao().upsert(TransactionEntity(id = discardEntityId, merchant = "Discard me", amountMinor = -1_000, syncState = "pending"))
            db.syncDao().enqueue(SyncOperation(kind = "transaction", entityId = discardEntityId, payload = "{}", state = "failed", attempts = 5))
            db.syncDao().failed().first { it.entityId == discardEntityId }.id
        }
        compose.onNodeWithTag("operational_health_refresh").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("local_sync_discard_$discardId").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("local_sync_discard_$discardId").performClick()
        compose.onNodeWithTag("local_sync_discard_confirm_$discardId").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            runBlocking(Dispatchers.IO) {
                db.syncDao().findById(discardId) == null &&
                    db.transactionDao().findById(discardEntityId) == null
            }
        }
    }

    @Test
    fun notificationDismissalRequiresConfirmationAndCanBeRestored() {
        val sourceRef = "dismiss-restore-ui"
        runBlocking(Dispatchers.IO) {
            LedgerDatabase.get(compose.activity).notificationDao().enqueue(
                NotificationRecord(
                    sourceRef = sourceRef,
                    packageName = "id.bmri.livin",
                    title = "Livin' by Mandiri",
                    body = "Pembayaran Rp10.000 di TOKO",
                    amountIdr = 10_000,
                    merchant = "TOKO",
                    bank = "LIVIN_MANDIRI",
                    reviewRequired = true,
                    receivedAt = System.currentTimeMillis(),
                ),
            )
        }
        compose.onNodeWithTag("nav_inbox").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("inbox_item_$sourceRef").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithText("Dismiss").performClick()
        compose.onNodeWithText("Dismiss capture?").assertIsDisplayed()
        compose.onNodeWithText("Cancel").performClick()
        runBlocking(Dispatchers.IO) {
            assertEquals("pending", LedgerDatabase.get(compose.activity).notificationDao().findBySourceRef(sourceRef)?.status)
        }
        compose.onNodeWithText("Dismiss").performClick()
        compose.onNodeWithTag("dismiss_confirm_$sourceRef").performClick()
        val dismissedId = runBlocking(Dispatchers.IO) {
            LedgerDatabase.get(compose.activity).notificationDao().findBySourceRef(sourceRef)!!.id
        }
        compose.waitUntil(timeoutMillis = 5_000) {
            runBlocking(Dispatchers.IO) {
                LedgerDatabase.get(compose.activity).notificationDao().findBySourceRef(sourceRef)?.status == "dismissed"
            }
        }
        compose.onNodeWithTag("nav_settings").performClick()
        compose.onNodeWithTag("open_diagnostics").performClick()
        compose.onNodeWithTag("diagnostics_list")
            .performScrollToNode(hasTestTag("diagnostic_capture_$dismissedId"))
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("restore_capture_$dismissedId")
                .fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("restore_capture_$dismissedId").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            runBlocking(Dispatchers.IO) {
                LedgerDatabase.get(compose.activity).notificationDao().findBySourceRef(sourceRef)?.status == "pending"
            }
        }
    }

    @Test
    fun transactionEditConflictSurvivesActivityRecreation() {
        val db = LedgerDatabase.get(compose.activity)
        val original = TransactionEntity(
            id = "detail-recreate",
            merchant = "Merchant",
            amountMinor = -5_000,
            description = "Original",
            category = "Food",
            account = "Cash",
            syncState = "synced",
        )
        runBlocking(Dispatchers.IO) { db.transactionDao().upsert(original) }
        compose.onNodeWithTag("nav_transactions").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_item_${original.id}").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("transaction_item_${original.id}").performClick()
        compose.onNodeWithTag("transaction_description").performTextClearance()
        compose.onNodeWithTag("transaction_description").performTextInput("Local unsaved edit")

        compose.activityRule.scenario.recreate()
        runBlocking(Dispatchers.IO) {
            db.transactionDao().upsert(original.copy(description = "Remote edit"))
        }
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_reload_remote").fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun transactionsCanSearchAndFilterByKind() {
        val db = LedgerDatabase.get(compose.activity)
        runBlocking(Dispatchers.IO) {
            db.transactionDao().upsert(
                TransactionEntity(
                    id = "instrumented-expense",
                    merchant = "Coffee Shop",
                    amountMinor = -45_000,
                    category = "Dining",
                    account = "Jago",
                    syncState = "synced",
                ),
            )
            db.transactionDao().upsert(
                TransactionEntity(
                    id = "instrumented-income",
                    merchant = "Salary Credit",
                    amountMinor = 5_000_000,
                    category = "Income",
                    account = "Mandiri",
                    syncState = "synced",
                ),
            )
        }

        compose.onNodeWithTag("nav_transactions").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_item_instrumented-expense")
                .fetchSemanticsNodes().isNotEmpty()
        }

        // All is selected initially and both seeded rows are visible.
        compose.onNodeWithText("Coffee Shop").assertIsDisplayed()
        compose.onNodeWithText("Salary Credit").assertIsDisplayed()
        compose.onNodeWithTag("transaction_refresh").performClick()
        compose.onNodeWithTag("transaction_sync_message").assertIsDisplayed()

        compose.onNodeWithTag("transaction_search").performTextInput("coffee")
        compose.onNodeWithText("Coffee Shop").assertIsDisplayed()
        assertEquals(0, compose.onAllNodesWithText("Salary Credit").fetchSemanticsNodes().size)
        compose.onNodeWithTag("transaction_search_clear").performClick()

        compose.onNodeWithTag("transaction_filter_expense").performClick()
        compose.onNodeWithText("Coffee Shop").assertIsDisplayed()
        assertEquals(0, compose.onAllNodesWithText("Salary Credit").fetchSemanticsNodes().size)

        compose.onNodeWithTag("transaction_filter_income").performClick()
        compose.onNodeWithText("Salary Credit").assertIsDisplayed()
        assertEquals(0, compose.onAllNodesWithText("Coffee Shop").fetchSemanticsNodes().size)

        compose.onNodeWithTag("transaction_filter_all").performClick()
        compose.onNodeWithText("Coffee Shop").assertIsDisplayed()
        compose.onNodeWithText("Salary Credit").assertIsDisplayed()
    }

    @Test
    fun syncedTransactionDetailScrollsToVoidAction() {
        val db = LedgerDatabase.get(compose.activity)
        runBlocking(Dispatchers.IO) {
            db.transactionDao().upsert(
                TransactionEntity(
                    id = "instrumented-detail",
                    merchant = "Scrollable Merchant",
                    description = "A transaction with a long editable form",
                    amountMinor = -125_000,
                    category = "Dining",
                    account = "Jago",
                    syncState = "synced",
                ),
            )
        }

        compose.onNodeWithTag("nav_transactions").performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_item_instrumented-detail")
                .fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("transaction_item_instrumented-detail").performClick()
        compose.onNodeWithTag("transaction_description").assertIsDisplayed()

        // Nested detail screens take over the full surface; the primary
        // navigation bar must not remain interactive underneath the form.
        listOf("dashboard", "inbox", "transactions", "budgets", "settings").forEach { route ->
            assertEquals(
                0,
                compose.onAllNodesWithTag("nav_$route").fetchSemanticsNodes().size,
            )
        }

        // The detail form is intentionally taller than the viewport; scroll the
        // parent to the destructive action and verify it is reachable.
        compose.onNodeWithTag("transaction_void").performScrollTo().assertIsDisplayed()

        compose.onNodeWithTag("transaction_back").performScrollTo().performClick()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("nav_transactions").fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("nav_transactions").assertIsDisplayed()
        compose.waitUntil(timeoutMillis = 5_000) {
            compose.onAllNodesWithTag("transaction_item_instrumented-detail")
                .fetchSemanticsNodes().isNotEmpty()
        }
        compose.onNodeWithTag("transaction_item_instrumented-detail").assertIsDisplayed()
    }
}
