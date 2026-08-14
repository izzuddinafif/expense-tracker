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
    /** Directional server kind; transfer principals are excluded from spend totals. */
    val kind: String = "expense",
    val ledgerRole: String = "ordinary",
    val transferBundleId: String? = null,
    val transferLeg: String? = null,
    /** Origin metadata from the authoritative ledger; references are retained for reconciliation only. */
    val source: String = "unknown",
    val sourceRef: String? = null,
    val evidenceCount: Int = 0,
)
