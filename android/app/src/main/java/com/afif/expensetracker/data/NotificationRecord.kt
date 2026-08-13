package com.afif.expensetracker.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(
    tableName = "ingestion_queue",
    indices = [Index(value = ["sourceRef"], unique = true)],
)
data class NotificationRecord(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val sourceRef: String,
    val packageName: String,
    val title: String,
    val body: String,
    val amountIdr: Long? = null,
    val merchant: String? = null,
    val bank: String = "UNKNOWN",
    val direction: String = "UNKNOWN",
    val occurredOn: String? = null,
    val reviewRequired: Boolean = true,
    val receivedAt: Long = System.currentTimeMillis(),
    val status: String = "pending",
    /** Links a same-content, different-key repost without suppressing either capture. */
    val suspectedDuplicateOf: Long? = null,
)
