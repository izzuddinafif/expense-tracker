package com.afif.expensetracker.manual

import com.afif.expensetracker.data.TransactionEntity
import java.util.Locale

/** Values shown as quick suggestions in the manual transaction form. */
data class ManualEntrySuggestionResult(
    val categories: List<String>,
    val accounts: List<String>,
)

/** Builds stable, local suggestions from the user's transaction history. */
object ManualEntrySuggestions {
    const val DEFAULT_LIMIT = 8

    private val accountFallbacks = listOf("BSI", "Mandiri", "Jago", "Cash")

    /**
     * Returns most recently used categories and accounts. Values are trimmed and
     * deduplicated without regard to case, while preserving the first spelling seen.
     * Account fallbacks are appended only when space remains.
     */
    fun build(
        transactions: List<TransactionEntity>,
        limit: Int = DEFAULT_LIMIT,
    ): ManualEntrySuggestionResult {
        val boundedLimit = limit.coerceAtLeast(0)
        val recent = transactions.sortedWith(
            compareByDescending<TransactionEntity> { it.occurredAt }
                .thenBy { it.id },
        )
        return ManualEntrySuggestionResult(
            categories = distinctRecent(recent, boundedLimit) { it.category },
            accounts = distinctRecent(recent, boundedLimit, accountFallbacks) { it.account },
        )
    }

    private fun distinctRecent(
        transactions: List<TransactionEntity>,
        limit: Int,
        fallbacks: List<String> = emptyList(),
        value: (TransactionEntity) -> String,
    ): List<String> {
        if (limit == 0) return emptyList()
        val result = ArrayList<String>(limit)
        val seen = HashSet<String>()

        fun append(raw: String) {
            if (result.size >= limit) return
            val trimmed = raw.trim()
            if (trimmed.isEmpty()) return
            val key = trimmed.lowercase(Locale.ROOT)
            if (seen.add(key)) result += trimmed
        }

        transactions.forEach { append(value(it)) }
        fallbacks.forEach(::append)
        return result
    }
}
