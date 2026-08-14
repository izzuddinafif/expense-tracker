package com.afif.expensetracker.data

import androidx.room.withTransaction
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId

/** Values entered while reviewing a notification before it becomes authoritative. */
data class NotificationConfirmationDraft(
    val merchant: String,
    val amountIdr: Long,
    val occurredOn: String,
    val description: String,
    val category: String,
    val account: String,
    val kind: String = "expense",
    val selfTransfer: Boolean = false,
)

internal fun NotificationConfirmationDraft.isValidForConfirmation(): Boolean {
    if (kind !in setOf("expense", "income")) return false
    if (amountIdr <= 0) return false
    if (merchant.isBlank() || description.isBlank()) return false
    if (category.isBlank() || account.isBlank()) return false
    return runCatching { LocalDate.parse(occurredOn) }.isSuccess
}

/**
 * Converts one reviewed notification into its local transaction and outbox
 * operation as a single commit. Repeated taps (or recomposition) are harmless.
 */
class NotificationConfirmationStore(private val database: LedgerDatabase) {
    suspend fun confirm(
        notificationId: Long,
        draft: NotificationConfirmationDraft? = null,
    ): Boolean = database.withTransaction {
        val record = database.notificationDao().findById(notificationId)
            ?: return@withTransaction false
        if (record.status != "pending") return@withTransaction false
        val transactionId = "android-${record.sourceRef}"
        val receivedOn = Instant.ofEpochMilli(record.receivedAt)
            .atZone(ZoneId.systemDefault())
            .toLocalDate()
            .toString()
        val values = draft ?: NotificationConfirmationDraft(
            merchant = record.merchant ?: record.title,
            amountIdr = record.amountIdr?.takeIf { it > 0 }
                ?: return@withTransaction false,
            occurredOn = record.occurredOn ?: receivedOn,
            description = record.title,
            category = "Uncategorized",
            account = record.bank,
            kind = if (record.direction == "CREDIT") "income" else "expense",
        )
        if (!values.isValidForConfirmation()) return@withTransaction false
        val occurredAt = LocalDate.parse(values.occurredOn.trim())
            .atStartOfDay(ZoneId.systemDefault())
            .toInstant()
            .toEpochMilli()

        database.transactionDao().upsert(
            TransactionEntity(
                id = transactionId,
                merchant = values.merchant.trim(),
                amountMinor = if (values.kind == "income") values.amountIdr else -values.amountIdr,
                description = values.description.trim(),
                category = values.category.trim(),
                account = values.account.trim(),
                occurredAt = occurredAt,
                syncState = "pending",
                kind = values.kind,
                ledgerRole = if (values.selfTransfer) "self_transfer_principal" else "ordinary",
            )
        )
        // The notification status gate above makes this insert idempotent while
        // Room's transaction serialization also protects concurrent taps.
        val payload = notificationConfirmationPayload(record, values)
        val latest = database.syncDao().findLatest("transaction", transactionId)
        if (latest?.state == "keep_review") {
            if (database.syncDao().requeueKeepReview(latest.id, payload) != 1) {
                database.syncDao().enqueue(
                    SyncOperation(kind = "transaction", entityId = transactionId, payload = payload),
                )
            }
        } else {
            database.syncDao().enqueue(
                SyncOperation(kind = "transaction", entityId = transactionId, payload = payload),
            )
        }
        database.notificationDao().updateStatus(record.id, "confirmed")
        true
    }
}

internal fun notificationConfirmationPayload(
    record: NotificationRecord,
    values: NotificationConfirmationDraft,
): String = JSONObject()
    .put("kind", values.kind)
    .put("amount_idr", values.amountIdr)
    .put("occurred_on", values.occurredOn.trim())
    .put("description", values.description.trim())
    .put("merchant", values.merchant.trim())
    .put("category", values.category.trim())
    .put("account", values.account.trim())
    .put("self_transfer", values.selfTransfer)
    .put("source_ref", record.sourceRef)
    .put("package_name", record.packageName)
    .put("received_at", record.receivedAt)
    .apply {
        val scheme = record.transferEvidenceScheme
        val reference = record.transferEvidenceReference
        if (!scheme.isNullOrBlank() && !reference.isNullOrBlank()) {
            put("transfer_evidence", JSONObject()
                .put("scheme", scheme)
                .put("reference", reference))
        }
    }
    .put("confirm", true)
    .toString()
