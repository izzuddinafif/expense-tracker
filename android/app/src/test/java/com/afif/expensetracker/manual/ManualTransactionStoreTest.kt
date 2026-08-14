package com.afif.expensetracker.manual

import androidx.room.DatabaseConfiguration
import androidx.room.InvalidationTracker
import androidx.room.RoomDatabase
import androidx.sqlite.db.SupportSQLiteOpenHelper
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.NotificationDao
import com.afif.expensetracker.data.NotificationRecord
import com.afif.expensetracker.data.SyncDao
import com.afif.expensetracker.data.SyncCheckpoint
import com.afif.expensetracker.data.SyncOperation
import com.afif.expensetracker.data.SyncRunLock
import com.afif.expensetracker.data.TransactionDao
import com.afif.expensetracker.data.TransactionEntity
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.emptyFlow
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ManualTransactionStoreTest {
    @Test
    fun expenseUsesNegativeAmountAndCompletePayload() = runBlocking {
        val database = RecordingDatabase()
        val id = ManualTransactionStore(
            database,
            idFactory = { "manual-expense" },
            transactionRunner = { block -> block() },
        ).create(
            ManualTransactionDraft(
                kind = ManualTransactionKind.EXPENSE,
                description = "Lunch",
                merchant = "Warung",
                amountIdr = 35_000,
                occurredOn = "2026-07-29",
                category = "Food",
                account = "Jago",
            )
        )

        assertEquals("manual-expense", id)
        val transaction = database.transactions.single()
        assertEquals(-35_000, transaction.amountMinor)
        assertEquals("pending", transaction.syncState)
        val payload = JSONObject(database.operations.single().payload)
        assertEquals("manual", payload.getString("source"))
        assertEquals("expense", payload.getString("kind"))
        assertEquals(35_000L, payload.getLong("amount_idr"))
        assertEquals("Food", payload.getString("subcategory"))
        assertEquals("manual-expense", payload.getString("source_ref"))
        assertTrue(payload.getBoolean("confirm"))
    }

    @Test
    fun incomeUsesPositiveAmount() = runBlocking {
        val database = RecordingDatabase()
        ManualTransactionStore(
            database,
            idFactory = { "manual-income" },
            transactionRunner = { block -> block() },
        ).create(
            ManualTransactionDraft(
                kind = ManualTransactionKind.INCOME,
                description = "Salary",
                merchant = "Employer",
                amountIdr = 7_500_000,
                occurredOn = "2026-07-01",
                category = "Salary",
                account = "Mandiri",
            )
        )

        assertEquals(7_500_000, database.transactions.single().amountMinor)
        assertEquals("income", JSONObject(database.operations.single().payload).getString("kind"))
    }

    @Test
    fun invalidDraftDoesNotWriteAnything() = runBlocking {
        val database = RecordingDatabase()
        val id = ManualTransactionStore(database).create(
            ManualTransactionDraft(
                kind = ManualTransactionKind.EXPENSE,
                description = " ",
                merchant = "Store",
                amountIdr = 0,
                occurredOn = "not-a-date",
                category = "Food",
                account = "BSI",
            )
        )

        assertEquals(null, id)
        assertTrue(database.transactions.isEmpty())
        assertTrue(database.operations.isEmpty())
    }

    @Test
    fun deterministicIdAndZoneAreApplied() = runBlocking {
        val database = RecordingDatabase()
        val id = ManualTransactionStore(
            database,
            idFactory = { "fixed-id" },
            zoneId = java.time.ZoneId.of("UTC"),
            transactionRunner = { block -> block() },
        ).create(
            ManualTransactionDraft(
                kind = ManualTransactionKind.INCOME,
                description = "Refund",
                merchant = "Store",
                amountIdr = 1_000,
                occurredOn = "2026-01-01",
                category = "Other",
                account = "BSI",
            )
        )

        assertEquals("fixed-id", id)
        assertEquals(1_767_225_600_000L, database.transactions.single().occurredAt)
    }

    @Test
    fun updateCompactsPendingCreateAndReplacesLocalRow() = runBlocking {
        val database = RecordingDatabase()
        val store = ManualTransactionStore(database, idFactory = { "manual-edit" }, transactionRunner = { block -> block() })
        store.create(ManualTransactionDraft(ManualTransactionKind.EXPENSE, "Lunch", "Warung", 35_000, "2026-07-29", "Food", "Jago"))
        assertEquals(PendingManualMutationResult.APPLIED, store.updatePendingManual("manual-edit", ManualTransactionDraft(ManualTransactionKind.EXPENSE, "Dinner", "Cafe", 50_000, "2026-07-28", "Food", "Jago")))
        assertEquals(1, database.operations.size)
        assertEquals("Dinner", JSONObject(database.operations.single().payload).getString("description"))
        assertEquals(-50_000, database.transactions.single().amountMinor)
    }

    @Test
    fun voidRemovesPendingCreateAndLocalRow() = runBlocking {
        val database = RecordingDatabase()
        val store = ManualTransactionStore(database, idFactory = { "manual-void" }, transactionRunner = { block -> block() })
        store.create(ManualTransactionDraft(ManualTransactionKind.INCOME, "Refund", "Store", 1_000, "2026-07-29", "Other", "Jago"))
        assertEquals(PendingManualMutationResult.APPLIED, store.voidPendingManual("manual-void"))
        assertTrue(database.operations.isEmpty())
        assertTrue(database.transactions.isEmpty())
    }

    @Test
    fun pendingMutationRejectsNonManualCreate() = runBlocking {
        val database = RecordingDatabase()
        val store = ManualTransactionStore(
            database,
            idFactory = { "notification-row" },
            transactionRunner = { block -> block() },
        )
        val original = ManualTransactionDraft(
            ManualTransactionKind.EXPENSE,
            "Lunch",
            "Warung",
            35_000,
            "2026-07-29",
            "Food",
            "Jago",
        )
        store.create(original)
        database.operations[0] = database.operations.single().copy(
            payload = JSONObject(database.operations.single().payload)
                .put("source", "android_notification")
                .toString(),
        )

        val result = store.updatePendingManual(
            "notification-row",
            original.copy(description = "Must not replace"),
        )

        assertEquals(PendingManualMutationResult.NOT_PENDING_MANUAL, result)
        assertEquals("Lunch", database.transactions.single().description)
        assertEquals(
            "android_notification",
            JSONObject(database.operations.single().payload).getString("source"),
        )
    }

    @Test
    fun pendingMutationRejectsCreateAlreadyClaimedForSync() = runBlocking {
        val database = RecordingDatabase()
        val store = ManualTransactionStore(
            database,
            idFactory = { "manual-sending" },
            transactionRunner = { block -> block() },
        )
        val draft = ManualTransactionDraft(
            ManualTransactionKind.INCOME,
            "Refund",
            "Store",
            1_000,
            "2026-07-29",
            "Other",
            "BSI",
        )
        store.create(draft)
        database.operations[0] = database.operations.single().copy(state = "sending")

        assertEquals(
            PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS,
            store.updatePendingManual("manual-sending", draft.copy(amountIdr = 2_000)),
        )
        assertEquals(
            PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS,
            store.voidPendingManual("manual-sending"),
        )
        assertEquals(1_000L, database.transactions.single().amountMinor)
        assertEquals("sending", database.operations.single().state)
    }

    @Suppress("DEPRECATION")
    private class RecordingDatabase : LedgerDatabase() {
        val transactions = mutableListOf<TransactionEntity>()
        val operations = mutableListOf<SyncOperation>()

        private val transactionDao = object : TransactionDao {
            override fun observeAll(): Flow<List<TransactionEntity>> = emptyFlow()
            override fun observeOccurredBetween(startInclusive: Long, endExclusive: Long): Flow<List<TransactionEntity>> = emptyFlow()
            override fun observeRecent(limit: Int): Flow<List<TransactionEntity>> = emptyFlow()
            override fun observeById(id: String): Flow<TransactionEntity?> = emptyFlow()
            override suspend fun findById(id: String): TransactionEntity? = transactions.find { it.id == id }
            override suspend fun upsert(transaction: TransactionEntity) {
                transactions.removeAll { it.id == transaction.id }
                transactions += transaction
            }
            override suspend fun delete(id: String) {
                transactions.removeAll { it.id == id }
            }
            override suspend fun updateSyncState(id: String, state: String) = Unit
        }
        private val syncDao = object : SyncDao {
            override suspend fun pending(): List<SyncOperation> = operations.toList()
            override suspend fun enqueue(operation: SyncOperation) { operations += operation }
            override suspend fun findLatest(kind: String, entityId: String): SyncOperation? =
                operations.lastOrNull { it.kind == kind && it.entityId == entityId }
            override suspend fun findPendingCreate(entityId: String): SyncOperation? =
                operations.lastOrNull { it.kind == "transaction" && it.entityId == entityId && it.state == "pending" }
            override suspend fun replacePendingCreatePayload(id: Long, payload: String, now: Long): Int {
                val index = operations.indexOfFirst { it.id == id && it.state == "pending" }
                if (index < 0) return 0
                operations[index] = operations[index].copy(payload = payload, updatedAt = now)
                return 1
            }
            override suspend fun discardPendingCreate(id: Long): Int {
                val index = operations.indexOfFirst { it.id == id && it.state == "pending" }
                if (index < 0) return 0
                operations.removeAt(index)
                return 1
            }
            override suspend fun claimPending(id: Long, claimToken: String, now: Long): Int = 0
            override suspend fun claimedOperation(id: Long, claimToken: String): SyncOperation? = null
            override suspend fun requeueClaimed(id: Long, claimToken: String, error: String, now: Long): Int = 0
            override suspend fun findById(id: Long): SyncOperation? = operations.find { it.id == id }
            override suspend fun requeueExpiredClaims(before: Long, now: Long): Int = 0
            override suspend fun pendingCount(): Int = operations.size
            override suspend fun failedCount(): Int = 0
            override suspend fun unsyncedEntityIds(): List<String> = operations.map { it.entityId }
            override suspend fun hasUnfinished(entityId: String): Boolean =
                operations.any {
                    it.entityId == entityId && it.state in setOf("pending", "failed", "sending")
                }
            override suspend fun oldestPendingAt(): Long? = operations.minOfOrNull { it.updatedAt }
            override suspend fun markSent(id: Long, claimToken: String, now: Long): Int = 0
            override suspend fun markFailure(id: Long, claimToken: String, error: String, maxAttempts: Int, now: Long): Int = 0
            override suspend fun sentCreates(limit: Int): List<SyncOperation> = emptyList()
            override suspend fun pruneSent(retain: Int, batchLimit: Int): Int = 0
            override suspend fun failed(): List<SyncOperation> = emptyList()
            override suspend fun requeueFailed(id: Long, now: Long): Int = 0
            override suspend fun discardFailed(id: Long): Int = 0
            override suspend fun checkpoint(feed: String): String? = null
            override suspend fun saveCheckpoint(checkpoint: SyncCheckpoint) = Unit
            override suspend fun ensureRunLock(lock: SyncRunLock) = Unit
            override suspend fun acquireRunLease(ownerToken: String, now: Long, leaseExpiresAt: Long): Int = 0
            override suspend fun ownedRunLease(ownerToken: String, now: Long): SyncRunLock? = null
            override suspend fun renewRunLease(ownerToken: String, generation: Long, now: Long, leaseExpiresAt: Long): Int = 0
            override suspend fun releaseRunLease(ownerToken: String, generation: Long): Int = 0
        }

        override fun transactionDao(): TransactionDao = transactionDao
        override fun syncDao(): SyncDao = syncDao
        override fun notificationDao(): NotificationDao = error("unused")
        override fun beginTransaction() = Unit
        override fun setTransactionSuccessful() = Unit
        override fun endTransaction() = Unit
        override fun clearAllTables() = Unit
        override fun createOpenHelper(config: DatabaseConfiguration): SupportSQLiteOpenHelper = error("unused")
        override fun createInvalidationTracker(): InvalidationTracker = InvalidationTracker(this)
    }
}
