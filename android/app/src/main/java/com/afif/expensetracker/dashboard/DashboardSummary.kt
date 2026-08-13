package com.afif.expensetracker.dashboard

import com.afif.expensetracker.data.TransactionEntity
import java.time.Instant
import java.time.YearMonth
import java.time.ZoneId

/** A presentation-ready aggregation for one local calendar month. */
data class DashboardCategoryTotal(
    val category: String,
    /** Positive IDR magnitude spent in this category. */
    val amountMinor: Long,
)

data class DashboardSummary(
    val month: YearMonth,
    val totalExpenseMinor: Long,
    val totalIncomeMinor: Long,
    val netMinor: Long,
    val transactionCount: Int,
    val topExpenseCategories: List<DashboardCategoryTotal>,
    val recentTransactions: List<TransactionEntity>,
)

/**
 * Computes a dashboard without touching Room or the clock unless the caller asks for defaults.
 * `occurredAt` is epoch milliseconds and is interpreted in [zone] for month membership.
 */
object DashboardSummaryCalculator {
    fun summarize(
        transactions: List<TransactionEntity>,
        month: YearMonth = YearMonth.now(),
        zone: ZoneId = ZoneId.systemDefault(),
        recentLimit: Int = DEFAULT_RECENT_LIMIT,
        categoryLimit: Int = DEFAULT_CATEGORY_LIMIT,
    ): DashboardSummary {
        val start = month.atDay(1).atStartOfDay(zone).toInstant()
        val end = month.plusMonths(1).atDay(1).atStartOfDay(zone).toInstant()
        return summarize(transactions, start, end, month, recentLimit, categoryLimit)
    }

    /** Uses an explicit half-open instant range, useful for deterministic tests and callers with UTC windows. */
    fun summarize(
        transactions: List<TransactionEntity>,
        startInclusive: Instant,
        endExclusive: Instant,
        month: YearMonth = YearMonth.from(startInclusive.atZone(ZoneId.of("UTC"))),
        recentLimit: Int = DEFAULT_RECENT_LIMIT,
        categoryLimit: Int = DEFAULT_CATEGORY_LIMIT,
    ): DashboardSummary {
        require(!endExclusive.isBefore(startInclusive)) { "endExclusive must not precede startInclusive" }
        val selected = transactions.filter { tx ->
            val occurred = Instant.ofEpochMilli(tx.occurredAt)
            occurred >= startInclusive && occurred < endExclusive
        }
        val expenses = selected.filter { it.amountMinor < 0L }
        val incomes = selected.filter { it.amountMinor > 0L }
        val totalExpense = expenses.fold(0L) { total, tx -> safeAdd(total, safeAbs(tx.amountMinor)) }
        val totalIncome = incomes.fold(0L) { total, tx -> safeAdd(total, tx.amountMinor) }
        val categories = expenses
            .groupingBy { it.category.ifBlank { "Uncategorized" } }
            .fold(0L) { total, tx -> safeAdd(total, safeAbs(tx.amountMinor)) }
            .map { (category, amount) -> DashboardCategoryTotal(category, amount) }
            // Kotlin's sortedWith is stable; equal totals retain first-seen category order.
            .sortedWith(compareByDescending<DashboardCategoryTotal> { it.amountMinor })
            .let { it.take(categoryLimit.coerceAtLeast(0)) }
        val recent = selected
            .sortedWith(compareByDescending<TransactionEntity> { it.occurredAt }.thenByDescending { it.id })
            .let { it.take(recentLimit.coerceAtLeast(0)) }

        return DashboardSummary(
            month = month,
            totalExpenseMinor = totalExpense,
            totalIncomeMinor = totalIncome,
            netMinor = safeSubtract(totalIncome, totalExpense),
            transactionCount = selected.size,
            topExpenseCategories = categories,
            recentTransactions = recent,
        )
    }

    private fun safeAbs(value: Long): Long = if (value == Long.MIN_VALUE) Long.MAX_VALUE else kotlin.math.abs(value)

    private fun safeAdd(left: Long, right: Long): Long =
        if (right > 0L && left > Long.MAX_VALUE - right) Long.MAX_VALUE
        else if (right < 0L && left < Long.MIN_VALUE - right) Long.MIN_VALUE
        else left + right

    private fun safeSubtract(left: Long, right: Long): Long =
        if (right > 0L && left < Long.MIN_VALUE + right) Long.MIN_VALUE
        else if (right < 0L && left > Long.MAX_VALUE + right) Long.MAX_VALUE
        else left - right

    private const val DEFAULT_RECENT_LIMIT = 5
    private const val DEFAULT_CATEGORY_LIMIT = 5
}
