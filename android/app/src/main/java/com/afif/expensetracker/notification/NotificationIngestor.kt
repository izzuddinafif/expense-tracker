package com.afif.expensetracker.notification

import com.afif.expensetracker.data.NotificationDao
import com.afif.expensetracker.data.NotificationRecord

/** Production seam shared by NotificationListenerService and deterministic tests. */
class NotificationIngestor(
    private val notificationDao: NotificationDao,
    private val now: () -> Long = System::currentTimeMillis,
) {
    suspend fun ingest(
        packageName: String,
        title: String,
        body: String,
        sourceIdentity: String? = null,
    ): Long? {
        if (!BankNotificationSources.isAllowlisted(packageName)) return null
        val parsed = BankNotificationParser.parse(packageName, title, body)
        val normalizedSourceIdentity = sourceIdentity?.takeIf(String::isNotBlank)
        val contentFingerprint = BankNotificationParser.fingerprint(
            parsed.packageName, parsed.title, parsed.body,
        )
        val platformIdentityRef = normalizedSourceIdentity?.let {
            BankNotificationParser.notificationIdentityRef(packageName, it, contentFingerprint)
        }
        platformIdentityRef?.let { identity ->
            notificationDao.findByPlatformIdentityRef(identity)?.let { existing ->
                refreshPending(existing.id, parsed)
                return existing.id
            }
        }
        val sourceRef = normalizedSourceIdentity?.let {
            BankNotificationParser.fingerprint(packageName, parsed.sourceRef, it)
        } ?: parsed.sourceRef
        val receivedAt = now()
        val suspectedDuplicateOf = notificationDao.findRecentForPackage(
            packageName = parsed.packageName,
            receivedAfter = receivedAt - SUSPECTED_REPOST_WINDOW_MILLIS,
            limit = SUSPECTED_REPOST_CANDIDATE_LIMIT,
        ).firstOrNull { existing ->
            existing.sourceRef != sourceRef &&
                BankNotificationParser.fingerprint(existing.packageName, existing.title, existing.body) == contentFingerprint
        }?.id
        val record = NotificationRecord(
            sourceRef = sourceRef,
            platformIdentityRef = platformIdentityRef,
            packageName = parsed.packageName,
            title = parsed.title,
            body = parsed.body,
            amountIdr = parsed.amountIdr,
            merchant = parsed.merchant,
            bank = parsed.bank.name,
            direction = parsed.direction.name,
            occurredOn = parsed.transactionDate?.toString(),
            reviewRequired = parsed.reviewRequired,
            receivedAt = receivedAt,
            suspectedDuplicateOf = suspectedDuplicateOf,
            transferEvidenceScheme = parsed.transferEvidence?.scheme,
            transferEvidenceReference = parsed.transferEvidence?.reference,
        )
        // Only sourceRef is an idempotency key. Equal payment text can represent
        // two legitimate purchases, so content/time-window matching must not drop it.
        val insertedId = notificationDao.enqueue(record)
        if (insertedId != -1L) return insertedId
        val existing = platformIdentityRef?.let { identity ->
            notificationDao.findByPlatformIdentityRef(identity)
        }
            ?: notificationDao.findBySourceRef(sourceRef)
            ?: error("Notification conflict could not be resolved")
        refreshPending(existing.id, parsed)
        return existing.id
    }

    private suspend fun refreshPending(id: Long, parsed: ParsedBankNotification) {
        notificationDao.refreshPendingCapture(
            id = id,
            title = parsed.title,
            body = parsed.body,
            amountIdr = parsed.amountIdr,
            merchant = parsed.merchant,
            bank = parsed.bank.name,
            direction = parsed.direction.name,
            occurredOn = parsed.transactionDate?.toString(),
            reviewRequired = parsed.reviewRequired,
            transferEvidenceScheme = parsed.transferEvidence?.scheme,
            transferEvidenceReference = parsed.transferEvidence?.reference,
        )
    }

    private companion object {
        const val SUSPECTED_REPOST_WINDOW_MILLIS = 30_000L
        const val SUSPECTED_REPOST_CANDIDATE_LIMIT = 20
    }
}
