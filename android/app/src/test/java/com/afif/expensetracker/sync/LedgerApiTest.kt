package com.afif.expensetracker.sync

import kotlin.test.Test
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class LedgerApiTest {
    @Test
    fun acceptedKeepReviewResponseBecomesDurableReviewDisposition() {
        val disposition = parsePushDisposition(
            202,
            """
            {"ingestion_outcome":{"code":"awaiting_canonical_email","action":"keep_review"}}
            """.trimIndent(),
        )

        assertTrue(disposition.keepReview)
        assertTrue(disposition.message.contains("remains in review"))
    }

    @Test
    fun malformedAcceptedResponseStillDoesNotInviteInfiniteRetry() {
        val disposition = parsePushDisposition(202, "not-json")

        assertTrue(disposition.keepReview)
    }

    @Test
    fun nonAcceptedResponseIsNotAReviewDisposition() {
        assertFalse(parsePushDisposition(500, "{}").keepReview)
    }
}
