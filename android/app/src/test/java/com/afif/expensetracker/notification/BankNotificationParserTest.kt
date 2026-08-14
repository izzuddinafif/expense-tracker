package com.afif.expensetracker.notification

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertNotEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class BankNotificationParserTest {
    @Test
    fun parsesBsiFixture() {
        val parsed = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE, "BYOND by BSI",
            "Pembayaran Rp 125.000 di TOKOPEDIA pada 28/07/2026",
        )
        assertEquals(Bank.BSI, parsed.bank)
        assertEquals(125000L, parsed.amountIdr)
        assertEquals("TOKOPEDIA", parsed.merchant)
        assertEquals("2026-07-28", parsed.transactionDate.toString())
        assertTrue(!parsed.reviewRequired)
    }

    @Test
    fun allowlistAcceptsBsiAndRejectsReplacedBcaSource() {
        assertTrue(BankNotificationSources.isAllowlisted(BankNotificationSources.BSI_BYOND_PACKAGE))
        assertTrue(BankNotificationSources.isAllowlisted(BankNotificationSources.LEGACY_BSI_MOBILE_PACKAGE))
        assertTrue(!BankNotificationSources.isAllowlisted("com.bca"))
    }

    @Test
    fun parsesLivinAndJagoFixtures() {
        val livin = BankNotificationParser.parse(
            "id.bmri.livin", "Livin' by Mandiri",
            "Transaksi berhasil Rp75.500 ke KOPI SENJA, 03/06/2026"
        )
        assertEquals(Bank.LIVIN_MANDIRI, livin.bank)
        assertEquals(75500L, livin.amountIdr)
        assertEquals("KOPI SENJA", livin.merchant)

        val jago = BankNotificationParser.parse(
            "com.jago.digitalBanking", "Jago",
            "Kamu melakukan pembayaran Rp 9.000 ke WARUNG PAGI pada 3 Jul 2026"
        )
        assertEquals(Bank.JAGO, jago.bank)
        assertEquals(9000L, jago.amountIdr)
        assertEquals("WARUNG PAGI", jago.merchant)
        assertEquals("2026-07-03", jago.transactionDate.toString())
    }

    @Test
    fun dateOnlyNotificationDoesNotTreatDateAsAmount() {
        val parsed = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "BYOND by BSI",
            "Pembayaran di TOKOPEDIA pada 28/07/2026",
        )

        assertNull(parsed.amountIdr)
        assertEquals("2026-07-28", parsed.transactionDate.toString())
        assertTrue(parsed.reviewRequired)
    }

    @Test
    fun accountSuffixAndBalanceDoNotBecomeTransactionAmounts() {
        val accountSuffix = BankNotificationParser.parse(
            "id.bmri.livin",
            "Livin' by Mandiri",
            "Pembayaran di TOKOPEDIA dari rekening ****1234",
        )
        val balance = BankNotificationParser.parse(
            "com.jago.digitalBanking",
            "Jago",
            "Saldo Anda Rp 1.250.000 pada rekening ****5678",
        )

        assertNull(accountSuffix.amountIdr)
        assertTrue(accountSuffix.reviewRequired)
        assertNull(balance.amountIdr)
        assertTrue(balance.reviewRequired)
    }

    @Test
    fun parsesPrefixAndSuffixCurrencyMarkers() {
        val prefix = BankNotificationParser.parse(
            "id.bmri.livin",
            "Livin' by Mandiri",
            "Pembayaran berhasil IDR 75.500 ke KOPI SENJA",
        )
        val suffix = BankNotificationParser.parse(
            "com.jago.digitalBanking",
            "Jago",
            "Pembayaran 9.000 rupiah ke WARUNG PAGI",
        )

        assertEquals(75500L, prefix.amountIdr)
        assertTrue(!prefix.reviewRequired)
        assertEquals(9000L, suffix.amountIdr)
        assertTrue(!suffix.reviewRequired)
    }

    @Test
    fun genericSuccessTemplateStaysInReview() {
        val parsed = BankNotificationParser.parse(
            "id.bmri.livin",
            "Livin' by Mandiri",
            "Transaksi berhasil Rp75.500 ke KOPI SENJA",
        )

        assertEquals(BankTransactionDirection.UNKNOWN, parsed.direction)
        assertTrue(parsed.reviewRequired)
    }

    @Test
    fun multipleCurrencyAmountsAreAmbiguousAndRequireReview() {
        val parsed = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "BYOND by BSI",
            "Pembayaran Rp 25.000 di TOKOPEDIA, biaya Rp 2.500",
        )

        assertNull(parsed.amountIdr)
        assertTrue(parsed.reviewRequired)
    }

    @Test
    fun sourceRefIsStableAndUnknownRemainsReviewable() {
        val first = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "Alert",
            "Rp 1.000 di TOKO",
        )
        val second = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "Alert",
            "Rp 1.000 di TOKO",
        )
        assertEquals(first.sourceRef, second.sourceRef)
        assertEquals(64, first.sourceRef.length)

        val unknown = BankNotificationParser.parse("com.example.bank", "Payment", "Rp 2.000 di TOKO")
        assertEquals(Bank.UNKNOWN, unknown.bank)
        assertTrue(unknown.reviewRequired)
        assertNotNull(unknown.amountIdr)
    }

    @Test
    fun notificationIdentitySeparatesReusedKeyWhenContentChanges() {
        val first = BankNotificationParser.notificationIdentityRef(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "0|co.id.bankbsi.superapp|42|null|10001",
        )
        val sameNotificationAfterContentUpdate = BankNotificationParser.notificationIdentityRef(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "0|co.id.bankbsi.superapp|42|null|10001",
            "content-a",
        )
        val reusedKeyWithNewContent = BankNotificationParser.notificationIdentityRef(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "0|co.id.bankbsi.superapp|42|null|10001",
            "content-b",
        )
        val distinctNotification = BankNotificationParser.notificationIdentityRef(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "0|co.id.bankbsi.superapp|43|null|10001",
        )

        assertNotEquals(first, sameNotificationAfterContentUpdate)
        assertNotEquals(sameNotificationAfterContentUpdate, reusedKeyWithNewContent)
        assertNotEquals(first, distinctNotification)
        assertEquals(64, first.length)
        assertFailsWith<IllegalArgumentException> {
            BankNotificationParser.notificationIdentityRef(
                BankNotificationSources.BSI_BYOND_PACKAGE,
                "   ",
            )
        }
    }

    @Test
    fun creditsAreNeverAutoSavedAsExpenses() {
        val parsed = BankNotificationParser.parse(
            "id.bmri.livin",
            "Livin' by Mandiri",
            "Dana masuk Rp 2.000.000 dari TRANSFER MASUK",
        )

        assertEquals(BankTransactionDirection.CREDIT, parsed.direction)
        assertTrue(parsed.reviewRequired)
    }

    @Test
    fun ambiguousDirectionRequiresReview() {
        val parsed = BankNotificationParser.parse(
            BankNotificationSources.BSI_BYOND_PACKAGE,
            "BYOND by BSI",
            "Rp 15.000 di TOKO SERBA ADA",
        )

        assertEquals(BankTransactionDirection.UNKNOWN, parsed.direction)
        assertTrue(parsed.reviewRequired)
    }
}
