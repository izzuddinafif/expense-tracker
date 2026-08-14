package com.afif.expensetracker.data

data class TransactionProvenance(val label: String, val detail: String)

/** Safe display copy: source references stay in Room for reconciliation and are never rendered. */
fun TransactionEntity.provenance(): TransactionProvenance {
    val label = when (source.lowercase()) {
        "manual" -> "Manual"
        "bank_notification", "notification" -> "Notifikasi bank"
        "bank_email", "email" -> "Email bank"
        "telegram" -> "Telegram"
        "gmail" -> "Gmail"
        else -> "Sumber ledger"
    }
    val evidence = evidenceCount.takeIf { it > 0 }?.let { "$it bukti terhubung" } ?: "Sumber dicatat oleh ledger"
    return TransactionProvenance(label, evidence)
}
