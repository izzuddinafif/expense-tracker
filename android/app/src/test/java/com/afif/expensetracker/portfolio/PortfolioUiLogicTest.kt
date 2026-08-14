package com.afif.expensetracker.portfolio

import com.afif.expensetracker.sync.PortfolioFreshness
import com.afif.expensetracker.sync.PortfolioAccount
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PortfolioUiLogicTest {
    @Test
    fun cachedAndPartialSnapshotsAreClearlyMarked() {
        val cached = portfolioFreshnessPresentation(PortfolioFreshness.LIVE, fromCache = true)
        val partial = portfolioFreshnessPresentation(PortfolioFreshness.PARTIAL, fromCache = false)

        assertEquals("Snapshot tersimpan", cached.label)
        assertTrue(cached.isAttention)
        assertEquals("Data sebagian", partial.label)
        assertTrue(partial.isAttention)
        assertFalse(portfolioFreshnessPresentation(PortfolioFreshness.LIVE, false).isAttention)
    }

    @Test
    fun idrFormatterUsesIndonesianCurrencyWithoutDecimals() {
        val formatted = formatIdr(1_250_000)
        assertTrue(formatted.startsWith("Rp"))
        assertFalse(formatted.contains(","))
    }

    @Test
    fun preferredAccountLabelsMatchNotionAccountSuffixes() {
        val accounts = listOf(
            PortfolioAccount("Mandiri 1854", "bank", 10L, 0L, 0L, 0L, "notion", null),
            PortfolioAccount("BSI 9400", "bank", 20L, 0L, 0L, 0L, "notion", null),
        )

        assertEquals("Mandiri 1854", dashboardAccountFor(accounts, "Mandiri")?.name)
        assertEquals("BSI 9400", dashboardAccountFor(accounts, "BSI")?.name)
    }
}
