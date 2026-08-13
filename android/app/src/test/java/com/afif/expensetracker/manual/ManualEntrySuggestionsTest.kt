package com.afif.expensetracker.manual

import com.afif.expensetracker.data.TransactionEntity
import org.junit.Assert.assertEquals
import org.junit.Test

class ManualEntrySuggestionsTest {
    private fun tx(
        id: String,
        category: String = "",
        account: String = "",
        occurredAt: Long,
    ) = TransactionEntity(
        id = id,
        merchant = "merchant",
        amountMinor = -1_000,
        category = category,
        account = account,
        occurredAt = occurredAt,
    )

    @Test
    fun ranksByMostRecentAndDeduplicatesCaseInsensitively() {
        val result = ManualEntrySuggestions.build(
            listOf(
                tx("old", category = "Food", account = "BSI", occurredAt = 1),
                tx("new", category = "  food ", account = "mandiri", occurredAt = 3),
                tx("mid", category = "Bills", account = "MANDIRI", occurredAt = 2),
            ),
        )

        assertEquals(listOf("food", "Bills"), result.categories)
        assertEquals(listOf("mandiri", "BSI", "Jago", "Cash"), result.accounts)
    }

    @Test
    fun ignoresBlankValuesAndKeepsCategoryWithoutFallbacks() {
        val result = ManualEntrySuggestions.build(
            listOf(tx("blank", category = "  ", account = "   ", occurredAt = 10)),
        )

        assertEquals(emptyList<String>(), result.categories)
        assertEquals(listOf("BSI", "Mandiri", "Jago", "Cash"), result.accounts)
    }

    @Test
    fun appliesDeterministicLimitToObservedValuesAndFallbacks() {
        val transactions = listOf(
            tx("a", category = "A", account = "Other", occurredAt = 3),
            tx("b", category = "B", account = "", occurredAt = 2),
        )

        assertEquals(
            listOf("A", "B"),
            ManualEntrySuggestions.build(transactions, limit = 2).categories,
        )
        assertEquals(
            listOf("Other", "BSI"),
            ManualEntrySuggestions.build(transactions, limit = 2).accounts,
        )
    }

    @Test
    fun zeroLimitReturnsNoSuggestions() {
        val result = ManualEntrySuggestions.build(
            listOf(tx("one", category = "Food", account = "BSI", occurredAt = 1)),
            limit = 0,
        )
        assertEquals(emptyList<String>(), result.categories)
        assertEquals(emptyList<String>(), result.accounts)
    }
}
