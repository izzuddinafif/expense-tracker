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
