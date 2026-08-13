package com.afif.expensetracker.transactions

import com.afif.expensetracker.data.TransactionEntity
import java.time.Instant
import java.time.YearMonth
import java.time.ZoneId
import java.util.Locale

/** The direction of a transaction, inferred from its signed minor-unit amount. */
enum class TransactionKind {
    ALL,
    EXPENSE,
    INCOME,
}

/** The granularity used for transaction section headers in the list UI. */
enum class TransactionGroupPeriod {
    DATE,
    MONTH,
}

data class TransactionGroup(
    /** ISO-8601 date (`2026-07-29`) or month (`2026-07`) presentation key. */
    val key: String,
    val transactions: List<TransactionEntity>,
)

/**
 * Applies the user-facing transaction filters and returns a deterministic newest-first list.
 * The same zone is used for both month matching and group labels, so boundary transactions do
 * not move between sections unexpectedly.
 */
fun filterTransactions(
    transactions: Iterable<TransactionEntity>,
    query: String = "",
    kind: TransactionKind = TransactionKind.ALL,
    month: YearMonth? = null,
    zoneId: ZoneId = ZoneId.systemDefault(),
): List<TransactionEntity> {
    val needle = query.trim().lowercase(Locale.ROOT)
    return transactions
        .asSequence()
        .filter { transaction ->
            val matchesQuery = needle.isEmpty() || listOf(
                transaction.merchant,
                transaction.description,
                transaction.category,
                transaction.account,
            ).any { it.lowercase(Locale.ROOT).contains(needle) }
            val matchesKind = when (kind) {
                TransactionKind.ALL -> true
                TransactionKind.EXPENSE -> transaction.amountMinor < 0
                TransactionKind.INCOME -> transaction.amountMinor > 0
            }
            val matchesMonth = month == null ||
                YearMonth.from(Instant.ofEpochMilli(transaction.occurredAt).atZone(zoneId)) == month
            matchesQuery && matchesKind && matchesMonth
        }
        .sortedWith(compareByDescending<TransactionEntity> { it.occurredAt }.thenByDescending { it.id })
        .toList()
}

/** Groups an already filtered list into stable, newest-first presentation sections. */
fun groupTransactions(
    transactions: Iterable<TransactionEntity>,
    period: TransactionGroupPeriod = TransactionGroupPeriod.DATE,
    zoneId: ZoneId = ZoneId.systemDefault(),
): List<TransactionGroup> {
    val sorted = transactions.sortedWith(
        compareByDescending<TransactionEntity> { it.occurredAt }.thenByDescending { it.id },
    )
    return sorted
        .groupBy { transaction ->
            val local = Instant.ofEpochMilli(transaction.occurredAt).atZone(zoneId)
            when (period) {
                TransactionGroupPeriod.DATE -> local.toLocalDate().toString()
                TransactionGroupPeriod.MONTH -> YearMonth.from(local).toString()
            }
        }
        .toList()
        .sortedByDescending { it.first }
        .map { (key, items) -> TransactionGroup(key, items) }
}
