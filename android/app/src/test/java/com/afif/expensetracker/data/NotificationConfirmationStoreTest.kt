package com.afif.expensetracker.data

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

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
}
