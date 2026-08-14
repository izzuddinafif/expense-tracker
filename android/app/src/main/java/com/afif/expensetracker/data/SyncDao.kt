package com.afif.expensetracker.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.Query

@Dao
interface SyncDao {
    @Query("SELECT * FROM sync_operations WHERE state = 'pending' ORDER BY id LIMIT 50") suspend fun pending(): List<SyncOperation>
    @Insert suspend fun enqueue(operation: SyncOperation)
    @Query("SELECT * FROM sync_operations WHERE kind = :kind AND entityId = :entityId ORDER BY id DESC LIMIT 1")
    suspend fun findLatest(kind: String, entityId: String): SyncOperation?
    @Query("SELECT * FROM sync_operations WHERE kind = 'transaction' AND entityId = :entityId AND state = 'pending' ORDER BY id DESC LIMIT 1")
    suspend fun findPendingCreate(entityId: String): SyncOperation?
    @Query("UPDATE sync_operations SET payload = :payload, updatedAt = :now WHERE id = :id AND kind = 'transaction' AND state = 'pending'")
    suspend fun replacePendingCreatePayload(id: Long, payload: String, now: Long = System.currentTimeMillis()): Int
    @Query("DELETE FROM sync_operations WHERE id = :id AND kind = 'transaction' AND state = 'pending'")
    suspend fun discardPendingCreate(id: Long): Int
    @Query("UPDATE sync_operations SET state = 'sending', claimToken = :claimToken, claimGeneration = claimGeneration + 1, lastAttemptAt = :now, updatedAt = :now WHERE id = :id AND state = 'pending'")
    suspend fun claimPending(id: Long, claimToken: String, now: Long = System.currentTimeMillis()): Int
    /** Transitional source-compatible overload; workers must pass a random fence. */
    @Deprecated("Sync workers must pass a random claim token")
    suspend fun claimPending(id: Long, now: Long): Int =
        claimPending(id, "legacy-$id-$now", now)
    @Query("SELECT * FROM sync_operations WHERE id = :id AND state = 'sending' AND claimToken = :claimToken LIMIT 1")
    suspend fun claimedOperation(id: Long, claimToken: String): SyncOperation?
    @Query("UPDATE sync_operations SET state = 'pending', claimToken = NULL, lastError = :error, updatedAt = :now WHERE id = :id AND state = 'sending' AND claimToken = :claimToken")
    suspend fun requeueClaimed(id: Long, claimToken: String, error: String, now: Long = System.currentTimeMillis()): Int
    @Query("SELECT * FROM sync_operations WHERE id = :id LIMIT 1")
    suspend fun findById(id: Long): SyncOperation?
    @Query("UPDATE sync_operations SET state = 'pending', claimToken = NULL, updatedAt = :now WHERE state = 'sending' AND (lastAttemptAt IS NULL OR lastAttemptAt < :before)")
    suspend fun requeueExpiredClaims(before: Long, now: Long = System.currentTimeMillis()): Int
    @Query("SELECT COUNT(*) FROM sync_operations WHERE state = 'pending'") suspend fun pendingCount(): Int
    @Query("SELECT COUNT(*) FROM sync_operations WHERE state = 'failed'") suspend fun failedCount(): Int
    @Query("SELECT DISTINCT entityId FROM sync_operations WHERE state IN ('pending', 'failed', 'sending')")
    suspend fun unsyncedEntityIds(): List<String>
    @Query("SELECT EXISTS(SELECT 1 FROM sync_operations WHERE entityId = :entityId AND state IN ('pending', 'failed', 'sending'))")
    suspend fun hasUnfinished(entityId: String): Boolean
    @Query("SELECT MIN(updatedAt) FROM sync_operations WHERE state = 'pending'") suspend fun oldestPendingAt(): Long?
    @Query("UPDATE sync_operations SET state = 'sent', attempts = attempts + 1, lastAttemptAt = :now, lastError = NULL, claimToken = NULL, updatedAt = :now WHERE id = :id AND state = 'sending' AND claimToken = :claimToken")
    suspend fun markSent(id: Long, claimToken: String, now: Long = System.currentTimeMillis()): Int
    @Query("UPDATE sync_operations SET state = CASE WHEN attempts + 1 >= :maxAttempts THEN 'failed' ELSE 'pending' END, attempts = attempts + 1, lastAttemptAt = :now, lastError = :error, claimToken = NULL, updatedAt = :now WHERE id = :id AND state = 'sending' AND claimToken = :claimToken")
    suspend fun markFailure(id: Long, claimToken: String, error: String, maxAttempts: Int = 5, now: Long = System.currentTimeMillis()): Int
    @Query("SELECT * FROM sync_operations WHERE kind = 'transaction' AND state = 'sent' ORDER BY id DESC LIMIT :limit")
    suspend fun sentCreates(limit: Int = 250): List<SyncOperation>
    @Query("DELETE FROM sync_operations WHERE id IN (SELECT id FROM sync_operations WHERE state = 'sent' ORDER BY updatedAt DESC, id DESC LIMIT :batchLimit OFFSET :retain)")
    suspend fun pruneSent(retain: Int = 200, batchLimit: Int = 50): Int
    @Query("SELECT * FROM sync_operations WHERE state = 'failed' ORDER BY updatedAt DESC, id DESC")
    suspend fun failed(): List<SyncOperation>
    @Query("UPDATE sync_operations SET state = 'pending', claimToken = NULL, lastError = NULL, updatedAt = :now WHERE id = :id AND state = 'failed'")
    suspend fun requeueFailed(id: Long, now: Long = System.currentTimeMillis()): Int
    @Query("DELETE FROM sync_operations WHERE id = :id AND state = 'failed'")
    suspend fun discardFailed(id: Long): Int

    @Query("SELECT cursor FROM sync_checkpoints WHERE feed = :feed LIMIT 1")
    suspend fun checkpoint(feed: String): String?
    @Insert(onConflict = androidx.room.OnConflictStrategy.REPLACE)
    suspend fun saveCheckpoint(checkpoint: SyncCheckpoint)

    @Insert(onConflict = androidx.room.OnConflictStrategy.IGNORE)
    suspend fun ensureRunLock(lock: SyncRunLock)
    @Query("UPDATE sync_run_lock SET ownerToken = :ownerToken, generation = generation + 1, leaseExpiresAt = :leaseExpiresAt WHERE id = 1 AND (ownerToken IS NULL OR leaseExpiresAt < :now OR ownerToken = :ownerToken)")
    suspend fun acquireRunLease(ownerToken: String, now: Long, leaseExpiresAt: Long): Int
    @Query("SELECT * FROM sync_run_lock WHERE id = 1 AND ownerToken = :ownerToken AND leaseExpiresAt >= :now LIMIT 1")
    suspend fun ownedRunLease(ownerToken: String, now: Long): SyncRunLock?
    @Query("UPDATE sync_run_lock SET leaseExpiresAt = :leaseExpiresAt WHERE id = 1 AND ownerToken = :ownerToken AND generation = :generation AND leaseExpiresAt >= :now")
    suspend fun renewRunLease(ownerToken: String, generation: Long, now: Long, leaseExpiresAt: Long): Int
    @Query("UPDATE sync_run_lock SET ownerToken = NULL, leaseExpiresAt = 0 WHERE id = 1 AND ownerToken = :ownerToken AND generation = :generation")
    suspend fun releaseRunLease(ownerToken: String, generation: Long): Int
}
