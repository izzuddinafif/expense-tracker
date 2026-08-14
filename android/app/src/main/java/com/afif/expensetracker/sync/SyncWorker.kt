package com.afif.expensetracker.sync

import android.content.Context
import android.util.Log
import androidx.room.withTransaction
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.data.SyncCheckpoint
import com.afif.expensetracker.data.SyncOperation
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val db = LedgerDatabase.get(applicationContext)
        val ownerToken = UUID.randomUUID().toString()
        val now = System.currentTimeMillis()
        db.syncDao().ensureRunLock(com.afif.expensetracker.data.SyncRunLock())
        if (db.syncDao().acquireRunLease(ownerToken, now, now + RUN_LEASE_MS) != 1) {
            // The immediate and periodic names are deliberately different, so
            // WorkManager alone cannot serialize them. The current owner will
            // drain the same durable outbox and feed.
            return@withContext Result.success()
        }
        val generation = db.syncDao().ownedRunLease(ownerToken, now)?.generation
            ?: return@withContext Result.retry()
        try {
            syncOwnedRun(db, ownerToken, generation)
        } finally {
            db.syncDao().releaseRunLease(ownerToken, generation)
        }
    }

    private suspend fun syncOwnedRun(db: LedgerDatabase, ownerToken: String, generation: Long): Result {
        db.syncDao().requeueExpiredClaims(System.currentTimeMillis() - CLAIM_TIMEOUT_MS)
        val settings = LedgerSettingsStore.read(applicationContext)
        val baseUrl = settings.baseUrl
        val token = settings.token
        if (baseUrl.isBlank() || token.isBlank()) return Result.failure()
        val api = LedgerApi(baseUrl, token)
        val feed = transactionFeedKey(baseUrl, token)
        var retryNeeded = false

        for (candidate in db.syncDao().pending()) {
            if (!renewLease(db, ownerToken, generation)) return Result.retry()
            val claimToken = UUID.randomUUID().toString()
            if (db.syncDao().claimPending(candidate.id, claimToken) != 1) continue
            val operation = db.syncDao().claimedOperation(candidate.id, claimToken) ?: continue
            when (operation.kind) {
                "transaction" -> {
                    val response = runCatching { api.push(operation.payload) }
                        .onFailure { Log.w(TAG, "Create failed for ${operation.entityId}", it) }
                    val canonical = response.getOrNull()
                    if (canonical == null) {
                        val error = response.exceptionOrNull()?.message ?: api.lastError ?: "create failed"
                        if (api.keepReview) {
                            if (!holdCreateForReview(db, ownerToken, generation, operation, claimToken, error)) {
                                return Result.retry()
                            }
                            continue
                        }
                        if (failClaim(db, ownerToken, generation, operation, claimToken, error)) {
                            retryNeeded = retryNeeded || operation.attempts + 1 < MAX_ATTEMPTS
                        }
                        continue
                    }
                    finishCreate(db, ownerToken, generation, operation, claimToken, canonical)
                }
                "transaction_update" -> {
                    val response = runCatching {
                        api.updateTransaction(operation.entityId, JSONObject(operation.payload))
                    }.onFailure { Log.w(TAG, "Update failed for ${operation.entityId}", it) }
                    val canonical = response.getOrNull()
                    if (canonical == null) {
                        val error = response.exceptionOrNull()?.message ?: api.lastError ?: "update failed"
                        if (failClaim(db, ownerToken, generation, operation, claimToken, error)) {
                            retryNeeded = retryNeeded || operation.attempts + 1 < MAX_ATTEMPTS
                        }
                        continue
                    }
                    finishUpdate(db, ownerToken, generation, operation, claimToken, canonical)
                }
                "transaction_void" -> {
                    val expectedUpdatedAt = runCatching {
                        JSONObject(operation.payload).optString("expected_updated_at")
                            .takeIf { it.isNotBlank() }
                    }.getOrNull()
                    val response = runCatching {
                        api.deleteTransaction(operation.entityId, expectedUpdatedAt)
                    }
                        .onFailure { Log.w(TAG, "Void failed for ${operation.entityId}", it) }
                    if (!response.getOrDefault(false)) {
                        val error = response.exceptionOrNull()?.message ?: api.lastError ?: "void failed"
                        if (failClaim(db, ownerToken, generation, operation, claimToken, error)) {
                            retryNeeded = retryNeeded || operation.attempts + 1 < MAX_ATTEMPTS
                        }
                        continue
                    }
                    finishVoid(db, ownerToken, generation, operation, claimToken)
                }
                else -> {
                    failClaim(db, ownerToken, generation, operation, claimToken, "unsupported operation: ${operation.kind}", 1)
                }
            }
        }

        val feedResult = runCatching { consumeChangeFeed(db, api, ownerToken, generation, feed) }
            .onFailure { Log.w(TAG, "Canonical change feed failed", it) }
        if (feedResult.isFailure) return Result.retry()
        return if (retryNeeded) Result.retry() else Result.success()
    }

    /**
     * A successful create is finalised in one Room transaction. In particular,
     * a process death cannot leave a sent outbox row without replacing its local
     * surrogate, which was the source of duplicate ledger rows.
     */
    private suspend fun finishCreate(
        db: LedgerDatabase,
        ownerToken: String,
        generation: Long,
        operation: SyncOperation,
        claimToken: String,
        canonical: com.afif.expensetracker.data.TransactionEntity,
    ): Boolean = db.withTransaction {
        if (!ownsLease(db, ownerToken, generation)) return@withTransaction false
        if (db.syncDao().markSent(operation.id, claimToken) != 1) return@withTransaction false
        if (canonical.id != operation.entityId) db.transactionDao().delete(operation.entityId)
        db.transactionDao().upsert(canonical)
        db.syncDao().pruneSent()
        true
    }

    private suspend fun finishUpdate(
        db: LedgerDatabase,
        ownerToken: String,
        generation: Long,
        operation: SyncOperation,
        claimToken: String,
        canonical: com.afif.expensetracker.data.TransactionEntity,
    ): Boolean = db.withTransaction {
        if (!ownsLease(db, ownerToken, generation)) return@withTransaction false
        if (db.syncDao().markSent(operation.id, claimToken) != 1) return@withTransaction false
        db.transactionDao().upsert(canonical)
        db.syncDao().pruneSent()
        true
    }

    private suspend fun finishVoid(
        db: LedgerDatabase,
        ownerToken: String,
        generation: Long,
        operation: SyncOperation,
        claimToken: String,
    ): Boolean = db.withTransaction {
        if (!ownsLease(db, ownerToken, generation)) return@withTransaction false
        if (db.syncDao().markSent(operation.id, claimToken) != 1) return@withTransaction false
        db.transactionDao().delete(operation.entityId)
        db.syncDao().pruneSent()
        true
    }

    private suspend fun failClaim(
        db: LedgerDatabase,
        ownerToken: String,
        generation: Long,
        operation: SyncOperation,
        claimToken: String,
        error: String,
        maxAttempts: Int = MAX_ATTEMPTS,
    ): Boolean = db.withTransaction {
        if (!ownsLease(db, ownerToken, generation)) return@withTransaction false
        db.syncDao().markFailure(operation.id, claimToken, error, maxAttempts) == 1
    }

    private suspend fun holdCreateForReview(
        db: LedgerDatabase,
        ownerToken: String,
        generation: Long,
        operation: SyncOperation,
        claimToken: String,
        error: String,
    ): Boolean = db.withTransaction {
        if (!ownsLease(db, ownerToken, generation)) return@withTransaction false
        if (db.syncDao().markKeepReview(operation.id, claimToken, error) != 1) return@withTransaction false
        db.transactionDao().updateSyncState(operation.entityId, "keep_review")
        val sourceRef = runCatching { JSONObject(operation.payload).optString("source_ref") }.getOrNull()
            ?.takeIf { it.isNotBlank() }
        sourceRef?.let { db.notificationDao().restoreForReview(it) }
        true
    }

    private suspend fun consumeChangeFeed(
        db: LedgerDatabase,
        api: LedgerApi,
        ownerToken: String,
        generation: Long,
        feed: String,
    ) {
        var cursor = db.syncDao().checkpoint(feed)
        val seenCursors = mutableSetOf<String>()
        while (true) {
            if (!renewLease(db, ownerToken, generation)) error("sync lease lost")
            val page = api.pullChanges(cursor) ?: error(api.lastError ?: "change feed failed")
            val sentCreatesBySource = db.syncDao().sentCreates().mapNotNull { operation ->
                val sourceRef = runCatching { JSONObject(operation.payload).optString("source_ref") }.getOrNull()
                    ?.takeIf { it.isNotBlank() }
                sourceRef?.let { it to operation }
            }.toMap()
            val cursorAfterPage = page.checkpointCursor ?: page.nextCursor
            db.withTransaction {
                if (!ownsLease(db, ownerToken, generation)) error("sync lease lost")
                for (change in page.transactions) {
                    val canonicalId = change.transaction.id
                    // This check and the following write must share one Room
                    // transaction: a local edit/create/void cannot be clobbered
                    // between observing its outbox state and applying the feed.
                    if (db.syncDao().hasUnfinished(canonicalId)) continue
                    val legacySurrogate = change.sourceRef?.let(sentCreatesBySource::get)
                    if (legacySurrogate != null &&
                        legacySurrogate.entityId != canonicalId &&
                        !db.syncDao().hasUnfinished(legacySurrogate.entityId)
                    ) {
                        // Recovery for the old non-atomic sent-before-replace
                        // sequence. The source_ref is server-idempotent.
                        db.transactionDao().delete(legacySurrogate.entityId)
                    }
                    if (change.status == "voided") {
                        db.transactionDao().delete(canonicalId)
                    } else if (change.status == "confirmed") {
                        db.transactionDao().upsert(change.transaction)
                    }
                }
                // Newer servers provide checkpoint_cursor even for a terminal
                // page. Older servers only have next_cursor; persisting that is
                // still safe, while a null terminal cursor intentionally falls
                // back to a replay rather than inventing an opaque cursor.
                cursorAfterPage?.let {
                    db.syncDao().saveCheckpoint(SyncCheckpoint(feed, it))
                }
            }
            val next = page.nextCursor ?: break
            if (!seenCursors.add(next)) error("change feed cursor repeated")
            cursor = next
        }
    }

    private suspend fun renewLease(db: LedgerDatabase, ownerToken: String, generation: Long): Boolean {
        val now = System.currentTimeMillis()
        return db.syncDao().renewRunLease(ownerToken, generation, now, now + RUN_LEASE_MS) == 1
    }

    private suspend fun ownsLease(db: LedgerDatabase, ownerToken: String, generation: Long): Boolean =
        db.syncDao().ownedRunLease(ownerToken, System.currentTimeMillis())?.generation == generation

    private companion object {
        const val TAG = "LedgerSyncWorker"
        const val MAX_ATTEMPTS = 5
        const val CLAIM_TIMEOUT_MS = 5 * 60 * 1000L
        const val RUN_LEASE_MS = 15 * 60 * 1000L
    }

    private fun transactionFeedKey(baseUrl: String, token: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest("${baseUrl.trimEnd('/')}\u001f$token".toByteArray())
            .joinToString("") { "%02x".format(it) }
        return "transactions:${digest.take(16)}"
    }
}
