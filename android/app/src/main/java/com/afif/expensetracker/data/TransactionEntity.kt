package com.afif.expensetracker.data

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "transactions")
data class TransactionEntity(
    @PrimaryKey val id: String,
    val merchant: String,
    val amountMinor: Long,
    val description: String = "",
    val currency: String = "IDR",
    val category: String = "Uncategorized",
    val account: String = "",
    val occurredAt: Long = System.currentTimeMillis(),
    val syncState: String = "pending",
    /** Server revision used to fence edits against a newer remote change. */
    val serverUpdatedAt: String? = null,
)
