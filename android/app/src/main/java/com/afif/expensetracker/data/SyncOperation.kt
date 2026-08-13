package com.afif.expensetracker.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "sync_operations")
data class SyncOperation(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val kind: String,
    val entityId: String,
    val payload: String,
    val state: String = "pending",
    val attempts: Int = 0,
    /** Epoch milliseconds when the most recent attempt started. */
    val lastAttemptAt: Long? = null,
    /** Last transport or validation error; cleared after a successful send. */
    val lastError: String? = null,
    /** Opaque per-claim fence. Only its holder may finish a sending operation. */
    val claimToken: String? = null,
    /** Monotonic claim revision, useful when diagnosing a recovered lease. */
    val claimGeneration: Long = 0,
    val updatedAt: Long = System.currentTimeMillis(),
)
