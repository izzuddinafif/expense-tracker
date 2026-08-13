package com.afif.expensetracker.manual

import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class ManualEntryFormattingTest {
    @Test
    fun idrPreviewGroupsDigitsDeterministically() {
        assertEquals("Rp7.500.000", formatIdrPreview("7500000"))
        assertEquals("Rp1", formatIdrPreview("0001"))
        assertEquals("Rp9.223.372.036.854.775.807", formatIdrPreview(Long.MAX_VALUE.toString()))
    }

    @Test
    fun idrPreviewRejectsInvalidZeroAndOverflow() {
        listOf("", "   ", "0", "0000", "12.000", "12a", "-1", "١٢", "１２").forEach {
            assertNull(formatIdrPreview(it), "expected invalid input: $it")
        }
        assertNull(formatIdrPreview("9223372036854775808"))
        assertNull(formatIdrPreview("999999999999999999999999999"))
    }

    @Test
    fun utcDateConversionRoundTrips() {
        listOf(
            LocalDate.of(1970, 1, 1),
            LocalDate.of(2024, 3, 10), // US DST transition date
            LocalDate.of(2024, 10, 27), // European DST transition date
            LocalDate.of(2099, 12, 31),
        ).forEach { date ->
            val millis = date.toUtcMidnightEpochMillis()
            assertEquals(date, utcMidnightEpochMillisToLocalDate(millis))
            assertEquals(date.atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli(), millis)
        }
    }

    @Test
    fun utcConversionDoesNotDependOnDstZones() {
        val date = LocalDate.of(2024, 3, 10)
        val expected = date.toUtcMidnightEpochMillis()
        listOf("America/New_York", "Europe/Berlin", "Asia/Jakarta").forEach { zone ->
            // Local midnight in a regional zone may have a different offset,
            // while DatePicker's UTC contract must remain unchanged.
            val regionalMillis = date.atStartOfDay(ZoneId.of(zone)).toInstant().toEpochMilli()
            assertEquals(expected, date.toUtcMidnightEpochMillis())
            assertEquals(date, utcMidnightEpochMillisToLocalDate(expected))
            if (zone != "Asia/Jakarta") check(regionalMillis != expected)
        }
    }
}
