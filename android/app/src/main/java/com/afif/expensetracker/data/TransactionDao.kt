package com.afif.expensetracker.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

@Dao
interface TransactionDao {
    @Query("SELECT * FROM transactions ORDER BY occurredAt DESC") fun observeAll(): Flow<List<TransactionEntity>>
    @Query("SELECT * FROM transactions WHERE occurredAt >= :startInclusive AND occurredAt < :endExclusive ORDER BY occurredAt DESC")
    fun observeOccurredBetween(startInclusive: Long, endExclusive: Long): Flow<List<TransactionEntity>>
    @Query("SELECT * FROM transactions ORDER BY occurredAt DESC LIMIT :limit")
    fun observeRecent(limit: Int): Flow<List<TransactionEntity>>
    @Query("SELECT * FROM transactions WHERE id = :id LIMIT 1")
    fun observeById(id: String): Flow<TransactionEntity?>
    @Query("SELECT * FROM transactions WHERE id = :id") suspend fun findById(id: String): TransactionEntity?
    @Insert(onConflict = OnConflictStrategy.REPLACE) suspend fun upsert(transaction: TransactionEntity)
    @Query("DELETE FROM transactions WHERE id = :id") suspend fun delete(id: String)
    @Query("UPDATE transactions SET syncState = :state WHERE id = :id") suspend fun updateSyncState(id: String, state: String)
}
