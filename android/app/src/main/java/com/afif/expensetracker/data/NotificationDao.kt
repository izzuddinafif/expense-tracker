package com.afif.expensetracker.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface NotificationDao {
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    suspend fun enqueue(record: NotificationRecord): Long
    @Query("SELECT * FROM ingestion_queue ORDER BY receivedAt DESC LIMIT :limit")
    fun observeRecent(limit: Int): Flow<List<NotificationRecord>>
    @Query("SELECT * FROM ingestion_queue WHERE status = :status ORDER BY receivedAt DESC LIMIT :limit")
    fun observeByStatus(status: String, limit: Int): Flow<List<NotificationRecord>>
    @Query("SELECT * FROM ingestion_queue WHERE id = :id") suspend fun findById(id: Long): NotificationRecord?
    @Query("SELECT * FROM ingestion_queue WHERE sourceRef = :sourceRef") suspend fun findBySourceRef(sourceRef: String): NotificationRecord?
    @Query("SELECT * FROM ingestion_queue WHERE platformIdentityRef = :platformIdentityRef")
    suspend fun findByPlatformIdentityRef(platformIdentityRef: String): NotificationRecord?
    @Query("SELECT * FROM ingestion_queue WHERE packageName = :packageName AND receivedAt >= :receivedAfter ORDER BY receivedAt DESC LIMIT :limit")
    suspend fun findRecentForPackage(packageName: String, receivedAfter: Long, limit: Int): List<NotificationRecord>
    @Query(
        """
        UPDATE ingestion_queue SET
            title = :title,
            body = :body,
            amountIdr = :amountIdr,
            merchant = :merchant,
            bank = :bank,
        direction = :direction,
        occurredOn = :occurredOn,
        reviewRequired = :reviewRequired,
        transferEvidenceScheme = :transferEvidenceScheme,
        transferEvidenceReference = :transferEvidenceReference
        WHERE id = :id AND status = 'pending'
        """,
    )
    suspend fun refreshPendingCapture(
        id: Long,
        title: String,
        body: String,
        amountIdr: Long?,
        merchant: String?,
        bank: String,
        direction: String,
        occurredOn: String?,
        reviewRequired: Boolean,
        transferEvidenceScheme: String?,
        transferEvidenceReference: String?,
    ): Int
    @Query("UPDATE ingestion_queue SET status = 'pending', reviewRequired = 1 WHERE sourceRef = :sourceRef")
    suspend fun restoreForReview(sourceRef: String): Int
    @Query("UPDATE ingestion_queue SET status = :status WHERE id = :id") suspend fun updateStatus(id: Long, status: String)
}
