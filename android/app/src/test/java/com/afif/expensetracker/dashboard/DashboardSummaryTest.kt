package com.afif.expensetracker.dashboard

import com.afif.expensetracker.data.TransactionEntity
import java.time.Instant
import java.time.YearMonth
import java.time.ZoneId
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class DashboardSummaryTest {
    private val month = YearMonth.of(2026, 7)
    private val zone = ZoneId.of("Asia/Jakarta")

    @Test
    fun excludesTransactionsOutsideSelectedLocalMonth() {
        val summary = DashboardSummaryCalculator.summarize(
            listOf(tx("before", -10_000, "2026-06-30T23:59:59+07:00"), tx("in", -20_000, "2026-07-01T00:00:00+07:00"), tx("after", 30_000, "2026-08-01T00:00:00+07:00")),
            month,
            zone,
        )

        assertEquals(1, summary.transactionCount)
        assertEquals(20_000, summary.totalExpenseMinor)
        assertEquals(listOf("in"), summary.recentTransactions.map { it.id })
    }

    @Test
    fun computesIncomeExpenseAndNet() {
        val summary = DashboardSummaryCalculator.summarize(listOf(tx("expense", -125_000), tx("income", 1_000_000)), month, zone)

        assertEquals(125_000, summary.totalExpenseMinor)
        assertEquals(1_000_000, summary.totalIncomeMinor)
        assertEquals(875_000, summary.netMinor)
        assertEquals(2, summary.transactionCount)
    }

    @Test
    fun transferPrincipalIsVisibleButExcludedFromCashflowTotals() {
        val summary = DashboardSummaryCalculator.summarize(
            listOf(
                tx("outgoing", -5_000_000, ledgerRole = "self_transfer_principal"),
                tx("incoming", 5_000_000, ledgerRole = "self_transfer_principal"),
                tx("fee", -2_500),
            ),
            month,
            zone,
        )

        assertEquals(2_500L, summary.totalExpenseMinor)
        assertEquals(0L, summary.totalIncomeMinor)
        assertEquals(-2_500L, summary.netMinor)
        assertEquals(3, summary.transactionCount)
    }

    @Test
    fun aggregatesCategoriesAndKeepsStableOrderForTies() {
        val summary = DashboardSummaryCalculator.summarize(
            listOf(tx("food-1", -100, category = "Food"), tx("travel", -200, category = "Travel"), tx("food-2", -100, category = "Food"), tx("other", -200, category = "Other")),
            month,
            zone,
            categoryLimit = 3,
        )

        assertEquals(listOf("Food", "Travel", "Other"), summary.topExpenseCategories.map { it.category })
        assertEquals(listOf(200L, 200L, 200L), summary.topExpenseCategories.map { it.amountMinor })
    }

    @Test
    fun recentTransactionsAreNewestFirstWithStableIdTieBreak() {
        val sameTime = Instant.parse("2026-07-15T00:00:00Z").toEpochMilli()
        val summary = DashboardSummaryCalculator.summarize(
            listOf(tx("a", -1, occurredAt = sameTime), tx("c", -1, occurredAt = sameTime + 1), tx("b", -1, occurredAt = sameTime)),
            Instant.parse("2026-07-01T00:00:00Z"),
            Instant.parse("2026-08-01T00:00:00Z"),
            month,
        )

        assertEquals(listOf("c", "b", "a"), summary.recentTransactions.map { it.id })
    }

    @Test
    fun localMonthBoundaryUsesZone() {
        val summary = DashboardSummaryCalculator.summarize(
            listOf(
                tx("last-june", -1, "2026-06-30T16:59:59Z"),
                tx("first-july", -2, "2026-06-30T17:00:00Z"),
                tx("last-july", -3, "2026-07-31T16:59:59Z"),
                tx("first-aug", -4, "2026-07-31T17:00:00Z"),
            ),
            month,
            zone,
        )

        assertEquals(listOf("last-july", "first-july"), summary.recentTransactions.map { it.id })
        assertEquals(2, summary.transactionCount)
    }

    @Test
    fun emptyStateHasZeroTotalsAndNoRows() {
        val summary = DashboardSummaryCalculator.summarize(emptyList(), month, zone)

        assertEquals(0, summary.transactionCount)
        assertEquals(0L, summary.totalExpenseMinor)
        assertEquals(0L, summary.totalIncomeMinor)
        assertEquals(0L, summary.netMinor)
        assertTrue(summary.topExpenseCategories.isEmpty())
        assertTrue(summary.recentTransactions.isEmpty())
    }

    private fun tx(
        id: String,
        amount: Long,
        occurred: String = "2026-07-10T12:00:00+07:00",
        occurredAt: Long? = null,
        category: String = "Food",
        ledgerRole: String = "ordinary",
    ) = TransactionEntity(id, "Merchant", amount, category = category, occurredAt = occurredAt ?: Instant.parse(occurred).toEpochMilli(), ledgerRole = ledgerRole)
}
