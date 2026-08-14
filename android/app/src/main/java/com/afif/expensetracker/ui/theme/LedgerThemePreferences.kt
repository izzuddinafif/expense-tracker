package com.afif.expensetracker.ui.theme

import android.content.Context

/** Persists the non-sensitive visual palette independently of connection credentials. */
object LedgerThemePreferences {
    private const val PREFERENCES = "ledger_ui_preferences"
    private const val THEME_PALETTE = "theme_palette"

    fun read(context: Context): LedgerThemePalette = LedgerThemePalette.fromStorageValue(
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            .getString(THEME_PALETTE, null),
    )

    fun save(context: Context, palette: LedgerThemePalette) {
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
            .edit()
            .putString(THEME_PALETTE, palette.storageValue)
            .apply()
    }

    fun clearForTests(context: Context) {
        context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE).edit().clear().commit()
    }
}
