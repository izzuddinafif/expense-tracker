package com.afif.expensetracker.settings

import java.net.URI
import java.net.URISyntaxException

/** Settings accepted by the Android client after validation and normalization. */
data class ValidatedSettings(
    val baseUrl: String,
    val token: String,
)

/** Result returned by [validateSettings] for use by the settings UI. */
sealed interface SettingsValidationResult {
    data class Valid(val settings: ValidatedSettings) : SettingsValidationResult

    data class Invalid(val message: String) : SettingsValidationResult
}

/**
 * Validates the manually entered API endpoint and bearer token.
 *
 * Release callers should retain the default HTTPS-only behavior. Debug builds
 * may explicitly allow LAN HTTP for development. A trailing slash is removed
 * so callers can append API paths without producing double slashes.
 */
fun validateSettings(
    baseUrl: String,
    token: String,
    allowCleartext: Boolean = false,
): SettingsValidationResult {
    val normalizedUrl = baseUrl.trim().trimEnd('/')
    val normalizedToken = token.trim()

    if (normalizedUrl.isEmpty()) {
        return SettingsValidationResult.Invalid("Enter an API base URL.")
    }
    if (normalizedToken.length < 32) {
        return SettingsValidationResult.Invalid("API token must be at least 32 characters.")
    }

    val parsed = try {
        URI(normalizedUrl)
    } catch (_: URISyntaxException) {
        return SettingsValidationResult.Invalid("API URL must use http:// or https://.")
    }

    val scheme = parsed.scheme
    if (!parsed.isAbsolute || scheme == null ||
        (!scheme.equals("http", ignoreCase = true) && !scheme.equals("https", ignoreCase = true))
    ) {
        return SettingsValidationResult.Invalid("API URL must use http:// or https://.")
    }
    if (parsed.host.isNullOrBlank()) {
        return SettingsValidationResult.Invalid("API URL must include a host.")
    }
    if (!parsed.rawUserInfo.isNullOrBlank() || !parsed.rawQuery.isNullOrBlank() || !parsed.rawFragment.isNullOrBlank()) {
        return SettingsValidationResult.Invalid("API URL must contain only the server origin.")
    }
    if (!parsed.path.isNullOrBlank() && parsed.path != "/") {
        return SettingsValidationResult.Invalid("API URL must contain only the server origin.")
    }
    if (scheme.equals("http", ignoreCase = true) && !allowCleartext) {
        return SettingsValidationResult.Invalid(
            "HTTPS is required outside debug builds.",
        )
    }

    return SettingsValidationResult.Valid(ValidatedSettings(normalizedUrl, normalizedToken))
}
