package com.afif.expensetracker.manual

import androidx.room.withTransaction
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.SyncOperation
import com.afif.expensetracker.data.TransactionEntity
import org.json.JSONObject
import java.time.LocalDate
import java.time.ZoneId
import java.util.UUID

enum class ManualTransactionKind {
    EXPENSE,
    INCOME,
}

data class ManualTransactionDraft(
    val kind: ManualTransactionKind,
    val description: String,
    val merchant: String,
    val amountIdr: Long,
    val occurredOn: String,
    val category: String,
    val account: String,
)

enum class PendingManualMutationResult {
    APPLIED,
    INVALID_DRAFT,
    NOT_PENDING_MANUAL,
    INITIAL_SYNC_IN_PROGRESS,
}

/** Creates a local manual transaction and its authoritative sync operation atomically. */
class ManualTransactionStore(
    private val database: LedgerDatabase,
    private val idFactory: () -> String = { "android-manual-${UUID.randomUUID()}" },
    private val zoneId: ZoneId = ZoneId.systemDefault(),
    private val transactionRunner: suspend (suspend () -> Unit) -> Unit = { block ->
        database.withTransaction { block() }
    },
) {
    suspend fun create(draft: ManualTransactionDraft): String? {
        val validated = validate(draft) ?: return null

        val id = idFactory()
        transactionRunner {
            upsertCreate(id, validated)
        }
        return id
    }

    suspend fun updatePendingManual(
        transactionId: String,
        draft: ManualTransactionDraft,
    ): PendingManualMutationResult {
        val validated = validate(draft) ?: return PendingManualMutationResult.INVALID_DRAFT
        var result = PendingManualMutationResult.NOT_PENDING_MANUAL
        transactionRunner {
            val latest = database.syncDao().findLatest("transaction", transactionId)
            val classification = classify(latest, transactionId)
            if (classification != null) {
                result = classification
                return@transactionRunner
            }
            val pending = database.syncDao().findPendingCreate(transactionId)
            if (pending == null || pending.id != latest?.id) {
                result = PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS
                return@transactionRunner
            }
            val replaced = database.syncDao().replacePendingCreatePayload(
                pending.id,
                payload(transactionId, validated),
            )
            if (replaced != 1) {
                result = PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS
                return@transactionRunner
            }
            database.transactionDao().upsert(entity(transactionId, validated))
            result = PendingManualMutationResult.APPLIED
        }
        return result
    }

    suspend fun voidPendingManual(transactionId: String): PendingManualMutationResult {
        var result = PendingManualMutationResult.NOT_PENDING_MANUAL
        transactionRunner {
            val latest = database.syncDao().findLatest("transaction", transactionId)
            val classification = classify(latest, transactionId)
            if (classification != null) {
                result = classification
                return@transactionRunner
            }
            val pending = database.syncDao().findPendingCreate(transactionId)
            if (pending == null || pending.id != latest?.id) {
                result = PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS
                return@transactionRunner
            }
            if (database.syncDao().discardPendingCreate(pending.id) != 1) {
                result = PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS
                return@transactionRunner
            }
            database.transactionDao().delete(transactionId)
            result = PendingManualMutationResult.APPLIED
        }
        return result
    }

    private data class Validated(
        val description: String,
        val merchant: String,
        val occurredOn: String,
        val category: String,
        val account: String,
        val occurredAt: Long,
        val signedAmount: Long,
        val amountIdr: Long,
        val kind: String,
    )

    private fun validate(draft: ManualTransactionDraft): Validated? {
        val description = draft.description.trim()
        val merchant = draft.merchant.trim()
        val occurredOn = draft.occurredOn.trim()
        val category = draft.category.trim()
        val account = draft.account.trim()
        if (draft.amountIdr <= 0 || description.isBlank() || merchant.isBlank() || occurredOn.isBlank() || category.isBlank() || account.isBlank()) return null
        val occurredAt = runCatching { LocalDate.parse(occurredOn).atStartOfDay(zoneId).toInstant().toEpochMilli() }.getOrNull() ?: return null
        val kind = if (draft.kind == ManualTransactionKind.EXPENSE) "expense" else "income"
        return Validated(description, merchant, occurredOn, category, account, occurredAt, if (kind == "expense") -draft.amountIdr else draft.amountIdr, draft.amountIdr, kind)
    }

    private fun entity(id: String, value: Validated) = TransactionEntity(id, value.merchant, value.signedAmount, value.description, category = value.category, account = value.account, occurredAt = value.occurredAt, syncState = "pending")

    private fun payload(id: String, value: Validated) = JSONObject().put("source", "manual").put("kind", value.kind).put("amount_idr", value.amountIdr).put("occurred_on", value.occurredOn).put("description", value.description).put("merchant", value.merchant).put("subcategory", value.category).put("account", value.account).put("source_ref", id).put("confirm", true).toString()

    private suspend fun upsertCreate(id: String, value: Validated) {
        database.transactionDao().upsert(entity(id, value))
        database.syncDao().enqueue(SyncOperation(kind = "transaction", entityId = id, payload = payload(id, value)))
    }

    private fun classify(operation: SyncOperation?, id: String): PendingManualMutationResult? {
        if (operation == null) return PendingManualMutationResult.NOT_PENDING_MANUAL
        if (operation.state == "sending" || operation.state == "claimed") return PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS
        if (operation.state != "pending" || operation.kind != "transaction") return PendingManualMutationResult.NOT_PENDING_MANUAL
        val json = runCatching { JSONObject(operation.payload) }.getOrNull() ?: return PendingManualMutationResult.NOT_PENDING_MANUAL
        if (json.optString("source") != "manual" || json.optString("source_ref") != id) return PendingManualMutationResult.NOT_PENDING_MANUAL
        return null
    }
}
