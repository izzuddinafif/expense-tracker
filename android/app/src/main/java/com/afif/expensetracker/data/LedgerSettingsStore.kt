package com.afif.expensetracker.data

import android.content.Context
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

data class LedgerConnectionSettings(val baseUrl: String, val token: String)

/** Stores the bearer token encrypted at rest and migrates the legacy plain preference on first read. */
object LedgerSettingsStore {
    private const val LEGACY_PREFS = "ledger_settings"
    private const val SECURE_PREFS = "ledger_secure_settings"
    private const val BASE_URL = "api_base_url"
    private const val TOKEN = "device_token"

    fun read(context: Context): LedgerConnectionSettings {
        val legacy = context.getSharedPreferences(LEGACY_PREFS, Context.MODE_PRIVATE)
        val secure = securePreferences(context)
        val legacyUrl = legacy.getString(BASE_URL, "").orEmpty()
        val legacyToken = legacy.getString(TOKEN, "").orEmpty()
        val baseUrl = secure.getString(BASE_URL, legacyUrl).orEmpty()
        val token = secure.getString(TOKEN, legacyToken).orEmpty()
        if (legacyToken.isNotBlank() && secure.getString(TOKEN, null).isNullOrBlank()) {
            secure.edit().putString(BASE_URL, baseUrl).putString(TOKEN, legacyToken).commit()
            legacy.edit().remove(TOKEN).apply()
        }
        return LedgerConnectionSettings(baseUrl, token)
    }

    fun save(context: Context, settings: LedgerConnectionSettings) {
        securePreferences(context).edit()
            .putString(BASE_URL, settings.baseUrl)
            .putString(TOKEN, settings.token)
            .commit()
        // Keep the non-secret endpoint available to existing debug tooling; remove only the secret.
        context.getSharedPreferences(LEGACY_PREFS, Context.MODE_PRIVATE).edit()
            .putString(BASE_URL, settings.baseUrl)
            .remove(TOKEN)
            .apply()
    }

    /** Test/support reset; production callers should overwrite settings instead. */
    fun clearForTests(context: Context) {
        securePreferences(context).edit().clear().commit()
        context.getSharedPreferences(LEGACY_PREFS, Context.MODE_PRIVATE).edit().clear().commit()
    }

    private fun securePreferences(context: Context) = EncryptedSharedPreferences.create(
        context,
        SECURE_PREFS,
        MasterKey.Builder(context).setKeyScheme(MasterKey.KeyScheme.AES256_GCM).build(),
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
    )
}
