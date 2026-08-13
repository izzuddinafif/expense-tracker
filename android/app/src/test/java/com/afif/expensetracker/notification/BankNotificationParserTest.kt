package com.afif.expensetracker.notification

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
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
}
