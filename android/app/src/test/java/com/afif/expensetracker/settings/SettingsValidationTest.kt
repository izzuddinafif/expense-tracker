package com.afif.expensetracker.settings

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class SettingsValidationTest {
    @Test
    fun acceptsLanHttpOnlyWhenDebugCallerOptsIn() {
        val result = validateSettings(
            "  http://192.168.1.20:8080///  ",
            "  secret-token  ",
            allowCleartext = true,
        )

        val valid = assertIs<SettingsValidationResult.Valid>(result)
        assertEquals("http://192.168.1.20:8080", valid.settings.baseUrl)
        assertEquals("secret-token", valid.settings.token)
    }

    @Test
    fun acceptsHttpsEndpoint() {
        val result = validateSettings("https://ledgerly.example/api", "token")

        assertIs<SettingsValidationResult.Valid>(result)
    }

    @Test
    fun rejectsLanHttpByDefault() {
        val result = validateSettings("http://192.168.1.20:8080", "token")

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("HTTPS"))
    }

    @Test
    fun rejectsMissingScheme() {
        val result = validateSettings("192.168.1.20:8080", "token")

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("http", ignoreCase = true))
    }

    @Test
    fun rejectsMissingHost() {
        val result = validateSettings("http:///api", "token")

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("host", ignoreCase = true))
    }

    @Test
    fun rejectsUnsupportedScheme() {
        val result = validateSettings("ftp://ledgerly.example", "token")

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("http", ignoreCase = true))
    }

    @Test
    fun rejectsBlankToken() {
        val result = validateSettings(
            "http://192.168.1.20:8080",
            "   ",
            allowCleartext = true,
        )

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("token", ignoreCase = true))
    }
}
