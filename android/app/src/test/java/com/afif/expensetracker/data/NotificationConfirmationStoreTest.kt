package com.afif.expensetracker.data

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import org.json.JSONObject

class NotificationConfirmationStoreTest {
    private val validDraft = NotificationConfirmationDraft(
        merchant = "Warung",
        amountIdr = 25_000,
        occurredOn = "2026-08-14",
        description = "Lunch",
        category = "Dining",
        account = "Cash",
    )

    @Test
    fun onlyExpenseAndIncomeKindsAreAccepted() {
        assertTrue(validDraft.copy(kind = "expense").isValidForConfirmation())
        assertTrue(validDraft.copy(kind = "income").isValidForConfirmation())
        assertFalse(validDraft.copy(kind = "refund").isValidForConfirmation())
        assertFalse(validDraft.copy(kind = "INCOME").isValidForConfirmation())
        assertFalse(validDraft.copy(kind = "").isValidForConfirmation())
    }

    @Test
    fun confirmationPayloadCarriesDurableTransferEvidenceWhenAvailable() {
        val payload = notificationConfirmationPayload(
            NotificationRecord(
                sourceRef = "capture-1",
                packageName = "id.bmri.livin",
                title = "Transfer",
                body = "Transfer bank",
                transferEvidenceScheme = "bank_reference",
                transferEvidenceReference = "TRX-JAGO-ABC12345",
            ),
            validDraft.copy(selfTransfer = true),
        )

        val evidence = JSONObject(payload).getJSONObject("transfer_evidence")
        assertEquals("bank_reference", evidence.getString("scheme"))
        assertEquals("TRX-JAGO-ABC12345", evidence.getString("reference"))
    }

    @Test
    fun confirmationPayloadOmitsIncompleteTransferEvidence() {
        val payload = notificationConfirmationPayload(
            NotificationRecord(
                sourceRef = "capture-2",
                packageName = "id.bmri.livin",
                title = "Transfer",
                body = "Transfer bank",
                transferEvidenceScheme = "bank_reference",
            ),
            validDraft.copy(selfTransfer = true),
        )

        assertFalse(JSONObject(payload).has("transfer_evidence"))
    }
}
