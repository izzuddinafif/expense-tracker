package com.afif.expensetracker.settings

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class SettingsValidationTest {
    private val validToken = "0123456789abcdef0123456789abcdef"

    @Test
    fun acceptsLanHttpOnlyWhenDebugCallerOptsIn() {
        val result = validateSettings(
            "  http://192.168.1.20:8080///  ",
            "  $validToken  ",
            allowCleartext = true,
        )

        val valid = assertIs<SettingsValidationResult.Valid>(result)
        assertEquals("http://192.168.1.20:8080", valid.settings.baseUrl)
        assertEquals(validToken, valid.settings.token)
    }

    @Test
    fun acceptsHttpsEndpoint() {
        val result = validateSettings("https://ledgerly.example/", validToken)

        val valid = assertIs<SettingsValidationResult.Valid>(result)
        assertEquals("https://ledgerly.example", valid.settings.baseUrl)
    }

    @Test
    fun rejectsPathQueryFragmentAndUserInfo() {
        listOf(
            "https://ledgerly.example/api",
            "https://ledgerly.example?next=/api",
            "https://ledgerly.example/#fragment",
            "https://user:pass@ledgerly.example",
        ).forEach { baseUrl ->
            val result = validateSettings(baseUrl, validToken)
            assertIs<SettingsValidationResult.Invalid>(result)
        }
    }

    @Test
    fun rejectsLanHttpByDefault() {
        val result = validateSettings("http://192.168.1.20:8080", validToken)

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("HTTPS"))
    }

    @Test
    fun rejectsMissingScheme() {
        val result = validateSettings("192.168.1.20:8080", validToken)

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("http", ignoreCase = true))
    }

    @Test
    fun rejectsMissingHost() {
        val result = validateSettings("http:///api", validToken)

        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("host", ignoreCase = true))
    }

    @Test
    fun rejectsUnsupportedScheme() {
        val result = validateSettings("ftp://ledgerly.example", validToken)

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

    @Test
    fun rejectsShortToken() {
        val result = validateSettings("https://ledgerly.example", "too-short-token")
        val invalid = assertIs<SettingsValidationResult.Invalid>(result)
        assertTrue(invalid.message.contains("32"))
    }
}
