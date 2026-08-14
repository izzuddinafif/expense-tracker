package com.afif.expensetracker.ui.theme

import kotlin.test.Test
import kotlin.test.assertEquals

class LedgerThemePaletteTest {
    @Test
    fun storageValuesRoundTripAndUnknownFallsBackSafely() {
        LedgerThemePalette.entries.forEach { palette ->
            assertEquals(palette, LedgerThemePalette.fromStorageValue(palette.storageValue))
        }
        assertEquals(LedgerThemePalette.DARK_GREEN, LedgerThemePalette.fromStorageValue(null))
        assertEquals(LedgerThemePalette.DARK_GREEN, LedgerThemePalette.fromStorageValue("removed-theme"))
    }
}
