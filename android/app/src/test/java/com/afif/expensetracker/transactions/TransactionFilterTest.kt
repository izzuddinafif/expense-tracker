package com.afif.expensetracker.transactions

import com.afif.expensetracker.data.TransactionEntity
import java.time.Instant
import java.time.YearMonth
import java.time.ZoneId
import kotlin.test.Test
import kotlin.test.assertEquals

class TransactionFilterTest {
    private val jakarta = ZoneId.of("Asia/Jakarta")

    @Test
    fun queryMatchesAllSearchableFieldsIgnoringCase() {
        val rows = listOf(
            tx("merchant", merchant = "Kopi Kenangan"),
            tx("description", description = "monthly SUBSCRIPTION"),
            tx("category", category = "Transport"),
            tx("account", account = "Jago Pocket"),
        )

        assertEquals("merchant", filterTransactions(rows, "KENANGAN").single().id)
        assertEquals("description", filterTransactions(rows, "subscription").single().id)
        assertEquals("category", filterTransactions(rows, "TRANSPORT").single().id)
        assertEquals("account", filterTransactions(rows, "jago pocket").single().id)
    }

    @Test
    fun kindAndBlankQueryFilters() {
        val rows = listOf(tx("expense", amount = -10), tx("income", amount = 20), tx("zero", amount = 0))
        assertEquals(listOf("zero", "income", "expense"), filterTransactions(rows).map { it.id })
        assertEquals(listOf("expense"), filterTransactions(rows, kind = TransactionKind.EXPENSE).map { it.id })
        assertEquals(listOf("income"), filterTransactions(rows, kind = TransactionKind.INCOME).map { it.id })
        assertEquals(emptyList(), filterTransactions(emptyList(), "anything"))
    }

    @Test
    fun transferPrincipalDoesNotAppearInExpenseOrIncomeFilters() {
        val transfer = tx("transfer", amount = -500_000).copy(ledgerRole = "self_transfer_principal")
        val ordinaryExpense = tx("expense", amount = -10)
        assertEquals(listOf("expense"), filterTransactions(listOf(transfer, ordinaryExpense), kind = TransactionKind.EXPENSE).map { it.id })
        assertEquals(listOf("transfer", "expense"), filterTransactions(listOf(transfer, ordinaryExpense)).map { it.id })
    }

    @Test
    fun monthFilterUsesRequestedTimezoneAtBoundaries() {
        val rows = listOf(
            tx("july-last", occurred = "2026-07-31T16:59:59Z"), // July 31 23:59:59 in Jakarta
            tx("august-first", occurred = "2026-07-31T17:00:00Z", amount = -1), // Aug 1 00:00
            tx("july-first", occurred = "2026-06-30T17:00:00Z"),
        )
        assertEquals(listOf("july-last", "july-first"), filterTransactions(rows, month = YearMonth.of(2026, 7), zoneId = jakarta).map { it.id })
        assertEquals(listOf("august-first"), filterTransactions(rows, month = YearMonth.of(2026, 8), zoneId = jakarta).map { it.id })
    }

    @Test
    fun sortIsNewestFirstAndUsesIdAsStableTieBreaker() {
        val rows = listOf(tx("b", occurred = "2026-07-01T00:00:00Z"), tx("a", occurred = "2026-07-01T00:00:00Z"), tx("old", occurred = "2025-01-01T00:00:00Z"))
        assertEquals(listOf("b", "a", "old"), filterTransactions(rows).map { it.id })
    }

    @Test
    fun combinedFiltersAndGroupingProducePresentationSections() {
        val rows = listOf(
            tx("food", merchant = "Food", category = "Dining", amount = -5, occurred = "2026-07-15T12:00:00Z"),
            tx("bus", merchant = "Bus", category = "Transport", amount = -3, occurred = "2026-07-15T09:00:00Z"),
            tx("income", merchant = "Salary", amount = 100, occurred = "2026-07-01T00:00:00Z"),
        )
        val filtered = filterTransactions(rows, query = "food", kind = TransactionKind.EXPENSE, month = YearMonth.of(2026, 7), zoneId = jakarta)
        assertEquals(listOf("food"), filtered.map { it.id })
        assertEquals(listOf("2026-07-15"), groupTransactions(filtered, zoneId = jakarta).map { it.key })
        assertEquals(listOf("2026-07"), groupTransactions(rows, TransactionGroupPeriod.MONTH, jakarta).map { it.key })
    }

    private fun tx(
        id: String,
        merchant: String = "Merchant",
        description: String = "",
        category: String = "Uncategorized",
        account: String = "",
        amount: Long = -10,
        occurred: String = "2026-07-01T00:00:00Z",
    ) = TransactionEntity(id, merchant, amount, description, category = category, account = account, occurredAt = Instant.parse(occurred).toEpochMilli())
}
