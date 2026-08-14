package com.afif.expensetracker.portfolio

import com.afif.expensetracker.sync.PortfolioFreshness
import java.text.NumberFormat
import java.util.Locale

fun formatIdr(value: Long): String = NumberFormat.getCurrencyInstance(Locale("id", "ID"))
    .apply { maximumFractionDigits = 0 }
    .format(value)

data class PortfolioFreshnessPresentation(val label: String, val isAttention: Boolean)

fun portfolioFreshnessPresentation(
    freshness: PortfolioFreshness,
    fromCache: Boolean,
): PortfolioFreshnessPresentation = when {
    fromCache -> PortfolioFreshnessPresentation("Snapshot tersimpan", true)
    freshness == PortfolioFreshness.PARTIAL -> PortfolioFreshnessPresentation("Data sebagian", true)
    freshness == PortfolioFreshness.CACHED -> PortfolioFreshnessPresentation("Data cache server", true)
    else -> PortfolioFreshnessPresentation("Live", false)
}

fun portfolioCacheAgeLabel(cachedAt: Long?, now: Long = System.currentTimeMillis()): String? {
    if (cachedAt == null || cachedAt <= 0L) return null
    val ageMinutes = ((now - cachedAt).coerceAtLeast(0L)) / 60_000L
    return when {
        ageMinutes < 1L -> "Disimpan kurang dari semenit lalu"
        ageMinutes < 60L -> "Disimpan ${ageMinutes} menit lalu"
        else -> "Disimpan ${ageMinutes / 60L} jam lalu"
    }
}
