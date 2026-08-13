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
    @Query("SELECT * FROM ingestion_queue WHERE packageName = :packageName AND receivedAt >= :receivedAfter ORDER BY receivedAt DESC LIMIT :limit")
    suspend fun findRecentForPackage(packageName: String, receivedAfter: Long, limit: Int): List<NotificationRecord>
    @Query("UPDATE ingestion_queue SET status = :status WHERE id = :id") suspend fun updateStatus(id: Long, status: String)
}
