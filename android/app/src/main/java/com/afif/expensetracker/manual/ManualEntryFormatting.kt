package com.afif.expensetracker.manual

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/**
 * Returns a stable Indonesian Rupiah preview for an amount entered as digits.
 * The input is deliberately kept as a string so a text field never has to
 * round-trip through a floating-point value. Invalid, zero, and overflowing
 * values return null.
 */
fun formatIdrPreview(digits: String): String? {
    val normalized = digits.trim()
    if (normalized.isEmpty() || normalized.any { it !in '0'..'9' }) return null

    val value = normalized.toLongOrNull() ?: return null
    if (value <= 0L) return null

    val grouped = normalized.trimStart('0')
        .reversed()
        .chunked(3)
        .map { it.reversed() }
        .reversed()
        .joinToString(".")
    return "Rp$grouped"
}

/** Material3 DatePicker-compatible UTC-midnight representation. */
fun LocalDate.toUtcMidnightEpochMillis(): Long =
    atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()

/** Inverse of [toUtcMidnightEpochMillis], independent of the device timezone. */
fun utcMidnightEpochMillisToLocalDate(epochMillis: Long): LocalDate =
    Instant.ofEpochMilli(epochMillis).atZone(ZoneOffset.UTC).toLocalDate()
