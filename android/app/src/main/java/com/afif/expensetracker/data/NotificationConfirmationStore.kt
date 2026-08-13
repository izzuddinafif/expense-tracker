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
)

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
        )
        if (!isValid(values)) return@withTransaction false
        val occurredAt = LocalDate.parse(values.occurredOn.trim())
            .atStartOfDay(ZoneId.systemDefault())
            .toInstant()
            .toEpochMilli()

        database.transactionDao().upsert(
            TransactionEntity(
                id = transactionId,
                merchant = values.merchant.trim(),
                amountMinor = -values.amountIdr,
                description = values.description.trim(),
                category = values.category.trim(),
                account = values.account.trim(),
                occurredAt = occurredAt,
            )
        )
        // The notification status gate above makes this insert idempotent while
        // Room's transaction serialization also protects concurrent taps.
        database.syncDao().enqueue(
            SyncOperation(
                kind = "transaction",
                entityId = transactionId,
                payload = JSONObject()
                    .put("kind", "expense")
                    .put("amount_idr", values.amountIdr)
                    .put("occurred_on", values.occurredOn.trim())
                    .put("description", values.description.trim())
                    .put("merchant", values.merchant.trim())
                    .put("category", values.category.trim())
                    .put("account", values.account.trim())
                    .put("source_ref", record.sourceRef)
                    .put("package_name", record.packageName)
                    .put("received_at", record.receivedAt)
                    .put("confirm", true)
                    .toString(),
            )
        )
        database.notificationDao().updateStatus(record.id, "confirmed")
        true
    }

    private fun isValid(draft: NotificationConfirmationDraft): Boolean {
        if (draft.amountIdr <= 0) return false
        if (draft.merchant.isBlank() || draft.description.isBlank()) return false
        if (draft.category.isBlank() || draft.account.isBlank()) return false
        val parsedDate = runCatching { LocalDate.parse(draft.occurredOn) }.getOrNull()
        return parsedDate != null
    }
}
