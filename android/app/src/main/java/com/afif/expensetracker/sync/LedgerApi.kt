package com.afif.expensetracker.sync

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import com.afif.expensetracker.data.TransactionEntity
import java.time.LocalDate
import java.time.ZoneId
import java.net.URLEncoder
import okhttp3.HttpUrl.Companion.toHttpUrl

class LedgerApi(private val baseUrl: String, private val deviceToken: String) {
    private val client = OkHttpClient()
    @Volatile var lastError: String? = null
        private set
    @Volatile var deferred: Boolean = false
        private set

    private fun rememberHttpFailure(response: okhttp3.Response) {
        val detail = when (response.code) {
            401, 403 -> "Authentication failed. Check the device token."
            404 -> "Ledger endpoint was not found. Check the API base URL."
            408, 504 -> "The ledger server timed out. Try again."
            429 -> "The ledger server is busy. Try again shortly."
            in 500..599 -> "The ledger server is temporarily unavailable."
            else -> "The ledger server rejected the request."
        }
        lastError = "HTTP ${response.code}: $detail"
    }

    /** Lightweight authenticated probe used by Settings before a user starts syncing. */
    fun health(): Boolean {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/health")
            .header("Authorization", "Bearer $deviceToken")
            .get()
            .build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                rememberHttpFailure(response)
                false
            } else {
                lastError = null
                true
            }
        }
    }

    fun push(payload: String): TransactionEntity? {
        deferred = false
        val url = baseUrl.trimEnd('/') + "/api/v1/transactions"
        val request = Request.Builder().url(url)
            .header("Authorization", "Bearer $deviceToken")
            .post(payload.toRequestBody("application/json".toMediaType())).build()
        return client.newCall(request).execute().use {
            if (it.code == 202) {
                deferred = true
                lastError = "HTTP 202: Waiting for the bank email to confirm this self-transfer."
                return@use null
            }
            if (!it.isSuccessful) {
                rememberHttpFailure(it)
                return@use null
            }
            lastError = null
            val body = it.body?.string() ?: return@use null
            parseTransaction(JSONObject(body).getJSONObject("transaction"))
        }
    }

    /** Fetch one page of the authoritative change feed. The cursor is opaque. */
    fun pullChanges(cursor: String? = null, limit: Int = 200): ChangePage? {
        val query = buildString {
            append("?limit=")
            append(limit.coerceIn(1, 200))
            if (!cursor.isNullOrBlank()) {
                append("&cursor=")
                append(URLEncoder.encode(cursor, Charsets.UTF_8.name()))
            }
        }
        val url = baseUrl.trimEnd('/') + "/api/v1/transactions/changes" + query
        val request = Request.Builder().url(url)
            .header("Authorization", "Bearer $deviceToken")
            .get().build()
        return client.newCall(request).execute().use {
            if (!it.isSuccessful) {
                rememberHttpFailure(it)
                return@use null
            }
            lastError = null
            val root = JSONObject(it.body?.string().orEmpty())
            val array = root.optJSONArray("transactions") ?: return@use ChangePage(emptyList(), null)
            val rows = buildList {
                for (index in 0 until array.length()) {
                    val value = array.getJSONObject(index)
                    add(
                        LedgerChange(
                            transaction = parseTransaction(value),
                            status = value.optString("status", "confirmed").lowercase(),
                            sourceRef = value.optString("source_ref").takeIf { it.isNotBlank() && it != "null" },
                        )
                    )
                }
            }
            ChangePage(
                transactions = rows,
                nextCursor = root.optString("next_cursor").takeIf { cursorValue -> cursorValue.isNotBlank() && cursorValue != "null" },
                checkpointCursor = root.optString("checkpoint_cursor").takeIf { cursorValue -> cursorValue.isNotBlank() && cursorValue != "null" },
            )
        }
    }

    /** Legacy capped pull retained for callers that do not need reconciliation. */
    fun pull(): List<TransactionEntity>? = pullChanges()?.transactions
        ?.filter { it.status == "confirmed" }
        ?.map { it.transaction }

    fun fetchTransaction(transactionId: String): RemoteTransaction? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/transactions/$transactionId")
            .header("Authorization", "Bearer $deviceToken")
            .get()
            .build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                rememberHttpFailure(response)
                return@use null
            }
            lastError = null
            val value = JSONObject(response.body?.string().orEmpty())
                .getJSONObject("transaction")
            RemoteTransaction(
                transaction = parseTransaction(value),
                voided = value.optString("status").equals("voided", ignoreCase = true),
            )
        }
    }

    /** Update editable transaction fields using the server's partial-update endpoint. */
    fun updateTransaction(transactionId: String, changes: JSONObject): TransactionEntity? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/transactions/$transactionId")
            .header("Authorization", "Bearer $deviceToken")
            .patch(changes.toString().toRequestBody("application/json".toMediaType()))
            .build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                rememberHttpFailure(response)
                return@use null
            }
            lastError = null
            val value = JSONObject(response.body?.string().orEmpty())
            parseTransaction(value.optJSONObject("transaction") ?: value)
        }
    }

    /** Void a transaction. The API returns no required body on success. */
    fun deleteTransaction(transactionId: String, expectedUpdatedAt: String? = null): Boolean {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/transactions/$transactionId")
            .header("Authorization", "Bearer $deviceToken")
            .apply { expectedUpdatedAt?.let { header("If-Match", it) } }
            .delete()
            .build()
        return client.newCall(request).execute().use {
            if (!it.isSuccessful) {
                rememberHttpFailure(it)
                false
            } else {
                lastError = null
                true
            }
        }
    }

    fun syncStatus(): SyncStatus? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/sync")
            .header("Authorization", "Bearer $deviceToken")
            .get().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            val value = JSONObject(response.body?.string().orEmpty())
            val errors = value.optJSONArray("recent_errors")
            SyncStatus(
                pendingCount = value.optInt("pending_count", 0),
                failedCount = value.optInt("failed_count", 0),
                oldestPendingAt = value.optString("oldest_pending_at").takeIf { it.isNotBlank() && it != "null" },
                recentErrors = buildList {
                    if (errors != null) for (index in 0 until errors.length()) {
                        val error = errors.optJSONObject(index) ?: continue
                        add(SyncError(
                            transactionId = error.optString("transaction_id").takeIf { it.isNotBlank() },
                            message = error.optString("last_error").takeIf { it.isNotBlank() } ?: "Unknown sync error",
                        ))
                    }
                },
            )
        }
    }

    fun retrySync(): Int? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/sync/retry")
            .header("Authorization", "Bearer $deviceToken")
            .post("".toRequestBody("application/json".toMediaType())).build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            JSONObject(response.body?.string().orEmpty()).optInt("retried", 0)
        }
    }

    fun operationalHealth(): OperationalHealth? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/ops/health")
            .header("Authorization", "Bearer $deviceToken")
            .get().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            val root = JSONObject(response.body?.string().orEmpty())
            val outbox = root.optJSONObject("outbox") ?: JSONObject()
            val workers = root.optJSONObject("workers") ?: JSONObject()
            OperationalHealth(
                status = root.optString("status", "unknown"),
                outboxDepth = outbox.optInt("depth", 0),
                outboxFailed = outbox.optInt("failed", 0),
                outboxStatus = outbox.optString("status", "unknown"),
                oldestPendingAt = outbox.optString("oldest_pending_at")
                    .takeIf { it.isNotBlank() && it != "null" },
                notion = parseWorkerHealth(workers.optJSONObject("notion_sync")),
                gmail = parseWorkerHealth(workers.optJSONObject("gmail")),
                backup = parseWorkerHealth(workers.optJSONObject("backup")),
                reconciliation = parseWorkerHealth(workers.optJSONObject("reconciliation")),
            )
        }
    }

    fun reconciliation(): ReconciliationStatus? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/reconciliation")
            .header("Authorization", "Bearer $deviceToken")
            .get().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            val value = JSONObject(response.body?.string().orEmpty())
            val duplicates = value.optJSONObject("duplicate_ids")
            ReconciliationStatus(
                clean = value.optBoolean("is_clean", false),
                discrepancyCount = listOf(
                    "missing_remote",
                    "unexpected_remote",
                    "kind_mismatches",
                    "notion_page_id_mismatches",
                    "voided_pages_still_active",
                ).sumOf { value.optJSONArray(it)?.length() ?: 0 } +
                    (duplicates?.length() ?: 0),
            )
        }
    }

    fun emailFailures(limit: Int = 20): List<EmailFailure>? {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/email-failures?limit=${limit.coerceIn(1, 100)}")
            .header("Authorization", "Bearer $deviceToken")
            .get().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) return@use null
            val array = JSONObject(response.body?.string().orEmpty())
                .optJSONArray("failures") ?: return@use emptyList()
            buildList {
                for (index in 0 until array.length()) {
                    val value = array.getJSONObject(index)
                    add(
                        EmailFailure(
                            uid = value.getString("uid"),
                            status = value.optString("status", "retrying"),
                            attempts = value.optInt("attempt_count", 0),
                            error = value.optString("last_error", "Unknown error"),
                        )
                    )
                }
            }
        }
    }

    fun retryEmailFailure(uid: String): Boolean {
        val encoded = URLEncoder.encode(uid, Charsets.UTF_8.name())
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/api/v1/email-failures/$encoded/retry")
            .header("Authorization", "Bearer $deviceToken")
            .post("".toRequestBody("application/json".toMediaType()))
            .build()
        return client.newCall(request).execute().use { it.isSuccessful }
    }

    /** Read server-authoritative monthly budget usage. */
    fun listBudgets(month: String): BudgetResponse? {
        val url = (baseUrl.trimEnd('/') + "/api/v1/budgets").toHttpUrl().newBuilder()
            .addQueryParameter("month", month).build()
        val request = Request.Builder().url(url).header("Authorization", "Bearer $deviceToken").get().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) { rememberHttpFailure(response); return@use null }
            lastError = null
            val root = JSONObject(response.body?.string().orEmpty())
            val rows = root.optJSONArray("budgets") ?: return@use BudgetResponse(root.optString("month", month), emptyList())
            BudgetResponse(root.optString("month", month), buildList {
                for (index in 0 until rows.length()) {
                    val value = rows.getJSONObject(index)
                    add(MonthlyBudget(
                        month = value.optString("month", root.optString("month", month)),
                        category = value.optString("category", value.optString("name", "Uncategorized")),
                        budgetIdr = value.optLong("amount_idr", value.optLong("budget_idr", value.optLong("budget", 0L))),
                        spentIdr = value.optLong("spent_idr", value.optLong("spent", 0L)),
                        remainingIdr = value.optLong("remaining_idr", 0L),
                        percentage = value.optDouble("percentage", 0.0),
                        status = value.optString("status", "ok"),
                    ))
                }
            })
        }
    }

    /** Create or replace one budget and return the complete refreshed report. */
    fun upsertBudget(month: String, category: String, amountIdr: Long): BudgetResponse? {
        val payload = JSONObject().put("month", month).put("category", category).put("amount_idr", amountIdr)
        val request = Request.Builder().url(baseUrl.trimEnd('/') + "/api/v1/budgets")
            .header("Authorization", "Bearer $deviceToken")
            .put(payload.toString().toRequestBody("application/json".toMediaType())).build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) { rememberHttpFailure(response); return@use null }
            lastError = null
            parseBudgetResponse(JSONObject(response.body?.string().orEmpty()), month)
        }
    }

    fun deleteBudget(month: String, category: String): BudgetResponse? {
        val url = (baseUrl.trimEnd('/') + "/api/v1/budgets").toHttpUrl().newBuilder()
            .addQueryParameter("month", month).addQueryParameter("category", category).build()
        val request = Request.Builder().url(url).header("Authorization", "Bearer $deviceToken").delete().build()
        return client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) { rememberHttpFailure(response); return@use null }
            lastError = null
            parseBudgetResponse(JSONObject(response.body?.string().orEmpty()), month)
        }
    }

    private fun parseBudgetResponse(root: JSONObject, fallbackMonth: String): BudgetResponse {
        val month = root.optString("month", fallbackMonth)
        val rows = root.optJSONArray("budgets") ?: return BudgetResponse(month, emptyList())
        return BudgetResponse(month, buildList {
            for (index in 0 until rows.length()) {
                val value = rows.getJSONObject(index)
                add(MonthlyBudget(month, value.optString("category", value.optString("name", "Uncategorized")),
                    value.optLong("amount_idr", value.optLong("budget_idr", value.optLong("budget", 0L))), value.optLong("spent_idr", value.optLong("spent", 0L)),
                    value.optLong("remaining_idr", 0L), value.optDouble("percentage", 0.0), value.optString("status", "ok")))
            }
        })
    }

    private fun parseWorkerHealth(value: JSONObject?): WorkerHealth? {
        if (value == null) return null
        return WorkerHealth(
            lastAttemptAt = value.optString("last_attempt_at").takeIf { it.isNotBlank() && it != "null" },
            lastSuccessAt = value.optString("last_success_at").takeIf { it.isNotBlank() && it != "null" },
            lastError = value.optString("last_error").takeIf { it.isNotBlank() && it != "null" },
            status = value.optString("status", "unknown"),
            reason = value.optString("reason").takeIf { it.isNotBlank() && it != "null" },
        )
    }

    private fun parseTransaction(value: JSONObject): TransactionEntity {
        val kind = value.getString("kind")
        val amount = value.getLong("amount_idr")
        val occurredAt = LocalDate.parse(value.getString("occurred_on"))
            .atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli()
        val amountMinor = when (kind) {
            "expense" -> -amount
            "income" -> amount
            else -> if (value.optString("transfer_leg") == "outgoing" || value.optString("description").contains("(keluar)")) -amount else amount
        }
        return TransactionEntity(
            id = value.getString("id"),
            merchant = value.optString("merchant").ifBlank { value.optString("description", "Transaction") },
            amountMinor = amountMinor,
            description = value.optString("description"),
            category = value.optString("subcategory").ifBlank { value.optString("category", "Uncategorized") },
            account = value.optString("account"),
            occurredAt = occurredAt,
            syncState = "synced",
            serverUpdatedAt = value.optString("updated_at").takeIf { it.isNotBlank() && it != "null" },
            kind = kind,
            ledgerRole = value.optString("ledger_role", "ordinary"),
            transferBundleId = value.optString("transfer_bundle_id").takeIf { it.isNotBlank() && it != "null" },
            transferLeg = value.optString("transfer_leg").takeIf { it.isNotBlank() && it != "null" },
        )
    }
}

data class SyncStatus(
    val pendingCount: Int,
    val failedCount: Int,
    val oldestPendingAt: String?,
    val recentErrors: List<SyncError>,
)

data class RemoteTransaction(
    val transaction: TransactionEntity,
    val voided: Boolean,
)

data class SyncError(val transactionId: String?, val message: String)

data class OperationalHealth(
    val status: String,
    val outboxDepth: Int,
    val outboxFailed: Int,
    val outboxStatus: String,
    val oldestPendingAt: String?,
    val notion: WorkerHealth?,
    val gmail: WorkerHealth?,
    val backup: WorkerHealth?,
    val reconciliation: WorkerHealth?,
)

data class WorkerHealth(
    val lastAttemptAt: String?,
    val lastSuccessAt: String?,
    val lastError: String?,
    val status: String,
    val reason: String?,
)

data class ReconciliationStatus(
    val clean: Boolean,
    val discrepancyCount: Int,
)

data class EmailFailure(
    val uid: String,
    val status: String,
    val attempts: Int,
    val error: String,
)

data class LedgerChange(val transaction: TransactionEntity, val status: String, val sourceRef: String? = null)

data class ChangePage(
    val transactions: List<LedgerChange>,
    val nextCursor: String?,
    /** Cursor after this response's final row; present even on a terminal page. */
    val checkpointCursor: String? = null,
)

data class MonthlyBudget(
    val month: String, val category: String, val budgetIdr: Long, val spentIdr: Long,
    val remainingIdr: Long, val percentage: Double, val status: String,
)

data class BudgetResponse(val month: String, val budgets: List<MonthlyBudget>)
