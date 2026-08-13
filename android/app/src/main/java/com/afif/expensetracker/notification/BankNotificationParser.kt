package com.afif.expensetracker.notification

import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.time.LocalDate
import java.util.Locale

/** A normalized, local-only representation of a bank notification. */
data class ParsedBankNotification(
    val packageName: String,
    val title: String,
    val body: String,
    val amountIdr: Long?,
    val merchant: String?,
    val transactionDate: LocalDate?,
    val sourceRef: String,
    val bank: Bank,
    val reviewRequired: Boolean,
) {
    /** Alias useful to ingestion clients that call the value a transaction date. */
    val date: LocalDate? get() = transactionDate
}

enum class Bank { BSI, LIVIN_MANDIRI, JAGO, UNKNOWN }

/**
 * Notification sources accepted for ingestion.
 *
 * Package identifiers are centralized here so they can be updated independently
 * when BSI migrates customers between apps.
 */
object BankNotificationSources {
    const val BSI_BYOND_PACKAGE = "co.id.bankbsi.superapp"
    const val LEGACY_BSI_MOBILE_PACKAGE = "com.bsm.activity2"

    val allowlistedPackages = setOf(
        BSI_BYOND_PACKAGE,
        LEGACY_BSI_MOBILE_PACKAGE,
        "id.bmri.livin",
        "com.jago.digitalBanking",
    )

    fun isAllowlisted(packageName: String): Boolean = packageName in allowlistedPackages
}

object BankNotificationParser {
    private val currencyAmountPattern = Regex(
        "(?i)(?:\\b(?:rp\\.?|idr|rupiah)\\s*([0-9]+(?:[.,][0-9]+)*)|" +
            "([0-9]+(?:[.,][0-9]+)*)\\s*(?:rp\\.?|idr|rupiah)\\b)",
    )
    private val balanceContextPattern = Regex("(?i)\\b(?:saldo|balance)\\b[^.!?;,]{0,60}$")
    private val datePattern = Regex("\\b(\\d{1,2})[/-](\\d{1,2})[/-](\\d{2,4})\\b")
    private val longDatePattern = Regex("(?i)\\b(\\d{1,2})\\s+(jan(?:uari)?|feb(?:ruari)?|mar(?:et)?|apr(?:il)?|mei|jun(?:i)?|jul(?:i)?|agu(?:stus)?|sep(?:t(?:ember)?)?|okt(?:ober)?|nov(?:ember)?|des(?:ember)?)\\s+(\\d{4})\\b")
    private val merchantPattern = Regex("(?i)\\b(?:di|ke|kepada|at|from)\\s+([A-Za-z0-9][A-Za-z0-9 .&'_-]{1,80}?)(?=\\s+(?:pada|tanggal|tgl|\\d{1,2}[/-]\\d{1,2})|[,.]|$)")

    fun parse(packageName: String, title: String, body: String): ParsedBankNotification {
        val normalizedPackage = packageName.trim()
        val normalizedTitle = title.trim()
        val normalizedBody = body.trim()
        val bank = identifyBank(normalizedPackage, normalizedTitle)
        val text = "$normalizedTitle $normalizedBody"
        val amount = extractAmount(text)
        val merchant = extractMerchant(text)
        return ParsedBankNotification(
            packageName = normalizedPackage,
            title = normalizedTitle,
            body = normalizedBody,
            amountIdr = amount,
            merchant = merchant,
            transactionDate = extractDate(text),
            sourceRef = fingerprint(normalizedPackage, normalizedTitle, normalizedBody),
            bank = bank,
            // Package allowlisting is only a coarse signal. An unexpected
            // template (or a partially redacted notification) must stay in
            // the review inbox instead of being silently treated as parsed.
            reviewRequired = bank == Bank.UNKNOWN || amount == null || merchant == null,
        )
    }

    fun fingerprint(packageName: String, title: String, body: String): String {
        val canonical = listOf(packageName, title, body).joinToString("\u001f") { it.trim().replace(Regex("\\s+"), " ") }
        return MessageDigest.getInstance("SHA-256")
            .digest(canonical.toByteArray(StandardCharsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
    }

    private fun identifyBank(packageName: String, title: String): Bank {
        // Body text may contain an unrelated merchant name (for example
        // "Mandiri Bookstore"), so bank identification uses package/title.
        val haystack = "$packageName $title".lowercase(Locale.ROOT)
        return when {
            packageName in setOf(
                BankNotificationSources.BSI_BYOND_PACKAGE,
                BankNotificationSources.LEGACY_BSI_MOBILE_PACKAGE,
            ) ||
                Regex("\\bbsi\\b").containsMatchIn(haystack) ||
                haystack.contains("bank syariah indonesia") -> Bank.BSI
            packageName.equals("id.bmri.livin", true) || haystack.contains("livin") || haystack.contains("mandiri") -> Bank.LIVIN_MANDIRI
            packageName.equals("com.jago.digitalBanking", true) || haystack.contains("jago") -> Bank.JAGO
            else -> Bank.UNKNOWN
        }
    }

    private fun extractAmount(text: String): Long? {
        val candidates = currencyAmountPattern.findAll(text)
            .filterNot { match -> isBalanceValue(text, match.range.first) }
            .map { match ->
                val rawAmount = match.groupValues[1].ifEmpty { match.groupValues[2] }
                rawAmount.filter(Char::isDigit).toLongOrNull()
            }
            .toList()

        // Without a verified bank-template match, only one explicitly marked
        // currency value is safe to accept. Multiple candidates are ambiguous
        // and must stay in the review flow.
        return candidates.singleOrNull()
    }

    private fun isBalanceValue(text: String, amountStart: Int): Boolean {
        val contextStart = (amountStart - 60).coerceAtLeast(0)
        return balanceContextPattern.containsMatchIn(text.substring(contextStart, amountStart))
    }

    private fun extractMerchant(text: String): String? = merchantPattern.find(text)?.groupValues?.get(1)?.trim()?.trimEnd('.', ',')?.takeIf { it.isNotBlank() }

    private fun extractDate(text: String): LocalDate? {
        datePattern.find(text)?.let {
            val day = it.groupValues[1].toIntOrNull() ?: return@let null
            val month = it.groupValues[2].toIntOrNull() ?: return@let null
            val rawYear = it.groupValues[3].toIntOrNull() ?: return@let null
            return runCatching { LocalDate.of(if (rawYear < 100) 2000 + rawYear else rawYear, month, day) }.getOrNull()
        }
        val long = longDatePattern.find(text) ?: return null
        val month = mapOf("jan" to 1, "feb" to 2, "mar" to 3, "apr" to 4, "mei" to 5, "jun" to 6, "jul" to 7, "agu" to 8, "sep" to 9, "okt" to 10, "nov" to 11, "des" to 12)
            .entries.firstOrNull { long.groupValues[2].startsWith(it.key, true) }?.value ?: return null
        return runCatching { LocalDate.of(long.groupValues[3].toInt(), month, long.groupValues[1].toInt()) }.getOrNull()
    }
}
