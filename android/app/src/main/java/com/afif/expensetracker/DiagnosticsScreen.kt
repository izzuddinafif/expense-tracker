@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.afif.expensetracker

import android.content.Context
import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.app.NotificationManagerCompat
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.afif.expensetracker.data.LedgerDatabase
import com.afif.expensetracker.data.NotificationRecord
import com.afif.expensetracker.data.SyncOperation
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.sync.SyncScheduler
import com.afif.expensetracker.sync.LedgerApi
import com.afif.expensetracker.sync.EmailFailure
import com.afif.expensetracker.sync.OperationalHealth
import com.afif.expensetracker.sync.ReconciliationStatus
import com.afif.expensetracker.sync.SyncStatus
import com.afif.expensetracker.ui.components.LedgerCard
import com.afif.expensetracker.ui.components.LedgerSectionHeader
import com.afif.expensetracker.ui.theme.Ink
import com.afif.expensetracker.ui.theme.Income
import com.afif.expensetracker.ui.theme.Warning
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import androidx.compose.runtime.rememberCoroutineScope
import androidx.work.WorkManager
import androidx.room.withTransaction

private data class LocalSyncHealth(
    val pending: Int,
    val failed: Int,
    val oldestPendingAt: Long?,
    val periodicState: String,
)

private data class SupportedBank(val name: String, val packageName: String)

private const val MAX_DIAGNOSTIC_ITEMS = 10

private val supportedBanks = listOf(
    SupportedBank("BSI BYOND", "co.id.bankbsi.superapp"),
    SupportedBank("Livin' by Mandiri", "id.bmri.livin"),
    SupportedBank("Jago", "com.jago.digitalBanking"),
)

private fun listenerEnabled(context: Context): Boolean = runCatching {
    NotificationManagerCompat.getEnabledListenerPackages(context).contains(context.packageName)
}.getOrDefault(false)

@Composable
fun DiagnosticsScreen(onBack: () -> Unit = {}) {
    val context = LocalContext.current
    val db = LedgerDatabase.get(context)
    val records by db.notificationDao().observeRecent(20).collectAsState(initial = emptyList())
    val scope = rememberCoroutineScope()
    var enabled by remember { mutableStateOf(listenerEnabled(context)) }
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, context) {
        val observer = androidx.lifecycle.LifecycleEventObserver { _, event ->
            if (event == androidx.lifecycle.Lifecycle.Event.ON_RESUME) enabled = listenerEnabled(context)
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    LazyColumn(
        Modifier.fillMaxSize().background(Ink).padding(20.dp).testTag("diagnostics_list"),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                IconButton(
                    onClick = onBack,
                    modifier = Modifier.testTag("diagnostics_back"),
                ) {
                    Icon(Icons.AutoMirrored.Rounded.ArrowBack, contentDescription = "Back")
                }
                Text(
                    "Notification diagnostics",
                    style = androidx.compose.material3.MaterialTheme.typography.headlineMedium,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier
                        .weight(1f)
                        .semantics { heading() },
                )
            }
            Text("Use this screen while validating bank notifications on a real device. Captured content stays on this device.", color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant)
        }
        item { OperationalHealthCard(context) }
        item { SyncDiagnosticsCard(context) }
        item {
            LedgerCard(modifier = Modifier.fillMaxWidth().testTag("notification_access_status")) {
                LedgerSectionHeader(
                    title = "Device capture",
                    subtitle = "Supported bank alerts stay local until you confirm them.",
                )
                Text(
                    if (enabled) "Notification access enabled" else "Notification access is disabled",
                    color = if (enabled) Income else Warning,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
                OutlinedButton(
                    modifier = Modifier.fillMaxWidth().testTag("open_notification_access"),
                    onClick = { context.startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) },
                ) { Text("Open notification access") }
            }
        }
        item { LedgerSectionHeader("Supported bank apps", "Only allowlisted packages can enter the review inbox.") }
        items(supportedBanks) { bank ->
            LedgerCard(
                modifier = Modifier.fillMaxWidth().testTag("supported_bank_${bank.packageName}"),
                contentPadding = 14.dp,
            ) {
                Text(bank.name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text(bank.packageName, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        item { LedgerSectionHeader("Recent captures", "Review status and restore dismissed captures.") }
        if (records.isEmpty()) item { Text("No supported notifications captured yet.", color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant) }
        items(records, key = { it.id }) { record ->
            CaptureRow(record) {
                scope.launch {
                    withContext(Dispatchers.IO) {
                        db.notificationDao().updateStatus(record.id, "pending")
                    }
                }
            }
        }
    }
}

@Composable
private fun OperationalHealthCard(context: Context) {
    val settings = remember { LedgerSettingsStore.read(context) }
    val baseUrl = settings.baseUrl
    val token = settings.token
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    var health by remember { mutableStateOf<OperationalHealth?>(null) }
    var localHealth by remember { mutableStateOf<LocalSyncHealth?>(null) }
    var reconciliation by remember { mutableStateOf<ReconciliationStatus?>(null) }
    var emailFailures by remember { mutableStateOf<List<EmailFailure>>(emptyList()) }
    var localFailures by remember { mutableStateOf<List<SyncOperation>>(emptyList()) }
    var discardLocalTarget by remember { mutableStateOf<SyncOperation?>(null) }
    var loading by remember { mutableStateOf(false) }
    var reconciliationLoading by remember { mutableStateOf(false) }

    fun refresh() {
        scope.launch {
            loading = true
            localHealth = withContext(Dispatchers.IO) {
                val db = LedgerDatabase.get(context)
                val work = runCatching {
                    WorkManager.getInstance(context)
                        .getWorkInfosForUniqueWork("ledger-sync-periodic").get()
                        .firstOrNull()?.state?.name
                }.getOrNull() ?: "NOT_SCHEDULED"
                LocalSyncHealth(
                    pending = db.syncDao().pendingCount(),
                    failed = db.syncDao().failedCount(),
                    oldestPendingAt = db.syncDao().oldestPendingAt(),
                    periodicState = work,
                )
            }
            localFailures = withContext(Dispatchers.IO) { LedgerDatabase.get(context).syncDao().failed() }
            health = if (baseUrl.isBlank() || token.isBlank()) null else {
                withContext(Dispatchers.IO) {
                    runCatching { LedgerApi(baseUrl, token).operationalHealth() }.getOrNull()
                }
            }
            emailFailures = if (baseUrl.isBlank() || token.isBlank()) emptyList() else {
                withContext(Dispatchers.IO) {
                    runCatching { LedgerApi(baseUrl, token).emailFailures() }
                        .getOrNull().orEmpty()
                }
            }
            loading = false
        }
    }

    fun reconcile() {
        if (baseUrl.isBlank() || token.isBlank()) return
        scope.launch {
            reconciliationLoading = true
            reconciliation = withContext(Dispatchers.IO) {
                runCatching { LedgerApi(baseUrl, token).reconciliation() }.getOrNull()
            }
            reconciliationLoading = false
        }
    }

    fun retryEmail(uid: String) {
        scope.launch {
            val retried = withContext(Dispatchers.IO) {
                runCatching { LedgerApi(baseUrl, token).retryEmailFailure(uid) }
                    .getOrDefault(false)
            }
            if (retried) refresh()
        }
    }
    fun retryLocal(operation: SyncOperation) {
        scope.launch {
            val requeued = withContext(Dispatchers.IO) {
                LedgerDatabase.get(context).syncDao().requeueFailed(operation.id)
            }
            if (requeued > 0) SyncScheduler.enqueue(context)
            refresh()
        }
    }
    fun discardLocal(operation: SyncOperation) {
        scope.launch {
            withContext(Dispatchers.IO) {
                val db = LedgerDatabase.get(context)
                db.withTransaction {
                    val current = db.syncDao().findById(operation.id)
                    if (current?.state == "failed") {
                        // A failed create has no canonical server record. Removing its outbox
                        // row alone would leave an un-syncable local surrogate behind.
                        if (current.kind == "transaction") db.transactionDao().delete(current.entityId)
                        db.syncDao().discardFailed(current.id)
                    }
                }
            }
            discardLocalTarget = null
            refresh()
        }
    }
    fun restoreRemote(operation: SyncOperation) {
        scope.launch {
            val remote = withContext(Dispatchers.IO) {
                runCatching { LedgerApi(baseUrl, token).fetchTransaction(operation.entityId) }
                    .getOrNull()
            }
            if (remote == null) {
                refresh()
                return@launch
            }
            withContext(Dispatchers.IO) {
                val db = LedgerDatabase.get(context)
                db.withTransaction {
                    if (remote.voided) db.transactionDao().delete(operation.entityId)
                    else db.transactionDao().upsert(remote.transaction)
                    db.syncDao().discardFailed(operation.id)
                }
            }
            refresh()
        }
    }
    androidx.compose.runtime.LaunchedEffect(baseUrl, token) { refresh() }

    LedgerCard(
        modifier = Modifier.fillMaxWidth().testTag("operational_health"),
        contentPadding = 16.dp,
    ) {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    "Operational health",
                    style = androidx.compose.material3.MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f).semantics { heading() },
                )
                OutlinedButton(
                    onClick = ::refresh,
                    enabled = !loading,
                    modifier = Modifier.testTag("operational_health_refresh"),
                ) { Text("Refresh") }
            }
            health?.let { value ->
                Text(
                    value.status.uppercase(),
                    color = if (value.status == "ok") Income else Warning,
                    modifier = Modifier
                        .testTag("operational_health_status")
                        .semantics { liveRegion = LiveRegionMode.Polite },
                )
                Text(
                    "Outbox: ${value.outboxDepth} pending · ${value.outboxFailed} failed · ${value.outboxStatus}",
                    modifier = Modifier.testTag("operational_health_outbox"),
                )
                WorkerHealthLine("Notion", value.notion, "ops_notion")
                WorkerHealthLine("Gmail", value.gmail, "ops_gmail")
                WorkerHealthLine("Backup", value.backup, "ops_backup")
                WorkerHealthLine("Reconciliation", value.reconciliation, "ops_reconciliation")
            } ?: Text(
                if (loading) "Loading…" else "Health data unavailable",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            )
            localHealth?.let { value ->
                Text(
                    "Device outbox: ${value.pending} pending · ${value.failed} failed",
                    modifier = Modifier.testTag("local_sync_outbox"),
                )
                Text(
                    "Periodic sync: ${value.periodicState}" +
                        (value.oldestPendingAt?.let { " · oldest $it" } ?: ""),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.testTag("local_sync_worker"),
                )
            }
            if (localFailures.isNotEmpty()) {
                HorizontalDivider()
                Text("Failed on this device", fontWeight = FontWeight.SemiBold)
                localFailures.take(MAX_DIAGNOSTIC_ITEMS).forEach { operation ->
                    Column(
                        Modifier.fillMaxWidth().testTag("local_sync_failure_${operation.id}"),
                        verticalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        Text("${operation.kind} · ${operation.attempts} attempts", color = Warning)
                        operation.lastError?.let { Text(sanitizeSyncError(it), color = MaterialTheme.colorScheme.onSurfaceVariant) }
                        FlowRow(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(4.dp),
                        ) {
                            OutlinedButton(
                                onClick = { retryLocal(operation) },
                                modifier = Modifier.testTag("local_sync_retry_${operation.id}"),
                            ) { Text("Retry") }
                            if (operation.kind == "transaction") {
                                OutlinedButton(
                                    onClick = { discardLocalTarget = operation },
                                    modifier = Modifier.testTag("local_sync_discard_${operation.id}"),
                                ) { Text("Discard") }
                            } else {
                                Text(
                                    "The local mutation is blocked. Restore the server copy or retry it.",
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    style = MaterialTheme.typography.bodySmall,
                                )
                                OutlinedButton(
                                    onClick = { restoreRemote(operation) },
                                    enabled = baseUrl.isNotBlank() && token.isNotBlank(),
                                    modifier = Modifier.testTag("local_sync_restore_${operation.id}"),
                                ) { Text("Restore server") }
                            }
                        }
                    }
                }
            }
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    reconciliation?.let {
                        if (it.clean) "Ledger and Notion match"
                        else "${it.discrepancyCount} reconciliation issue(s)"
                    } ?: "Reconciliation not run",
                    color = if (reconciliation?.clean == false) Warning else MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .weight(1f)
                        .testTag("reconciliation_status")
                        .semantics { liveRegion = LiveRegionMode.Polite },
                )
                OutlinedButton(
                    onClick = ::reconcile,
                    enabled = !reconciliationLoading && baseUrl.isNotBlank() && token.isNotBlank(),
                    modifier = Modifier.testTag("reconciliation_refresh"),
                ) { Text(if (reconciliationLoading) "Checking…" else "Check") }
            }
            if (emailFailures.isNotEmpty()) {
                HorizontalDivider()
                Text("Email processing failures", fontWeight = FontWeight.SemiBold)
                emailFailures.take(MAX_DIAGNOSTIC_ITEMS).forEach { failure ->
                    Column(
                        Modifier.fillMaxWidth().testTag("email_failure_${failure.uid}"),
                        verticalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        Text(
                            "UID ${failure.uid} · ${failure.status} · ${failure.attempts} attempts",
                            color = Warning,
                        )
                        Text(
                            sanitizeSyncError(failure.error),
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        OutlinedButton(
                            onClick = { retryEmail(failure.uid) },
                            modifier = Modifier.testTag("email_failure_retry_${failure.uid}"),
                        ) { Text("Retry email") }
                    }
                }
                val hiddenFailures = emailFailures.size - MAX_DIAGNOSTIC_ITEMS
                if (hiddenFailures > 0) {
                    Text(
                        "$hiddenFailures more not shown",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.testTag("email_failures_truncated"),
                    )
                }
            }
        }
    }
    discardLocalTarget?.let { operation ->
        androidx.compose.material3.AlertDialog(
            onDismissRequest = { discardLocalTarget = null },
            title = { Text("Discard failed operation?") },
            text = { Text("This deletes the failed unsynced transaction from this device and stops retrying it. Confirmed transactions can only be recovered by syncing again.") },
            confirmButton = { TextButton(onClick = { discardLocal(operation) }, modifier = Modifier.testTag("local_sync_discard_confirm_${operation.id}")) { Text("Discard") } },
            dismissButton = { TextButton(onClick = { discardLocalTarget = null }) { Text("Cancel") } },
        )
    }
}

@Composable
private fun WorkerHealthLine(
    label: String,
    health: com.afif.expensetracker.sync.WorkerHealth?,
    tag: String,
) {
    val detail = when {
        health == null -> "No heartbeat yet"
        health.reason != null -> "${health.status}: ${sanitizeSyncError(health.reason)}"
        health.lastError != null -> "Error: ${sanitizeSyncError(health.lastError)}"
        health.lastSuccessAt != null -> "${health.status}: ${health.lastSuccessAt}"
        else -> "No heartbeat yet"
    }
    Text(
        "$label — $detail",
        color = if (health?.status in setOf("degraded", "critical")) Warning
            else MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.testTag(tag),
    )
}

private fun sanitizeSyncError(message: String): String = message
    .replace(Regex("\\s+"), " ")
    .trim()
    .take(180)

@Composable
private fun SyncDiagnosticsCard(context: Context) {
    val settings = remember { LedgerSettingsStore.read(context) }
    val baseUrl = settings.baseUrl
    val token = settings.token
    val scope = androidx.compose.runtime.rememberCoroutineScope()
    var status by remember { mutableStateOf<SyncStatus?>(null) }
    var loading by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }

    fun refresh() {
        if (baseUrl.isBlank() || token.isBlank()) {
            message = "Set the API URL and device token in Settings."
            return
        }
        scope.launch {
            loading = true
            message = null
            val result = withContext(Dispatchers.IO) { runCatching { LedgerApi(baseUrl, token).syncStatus() }.getOrNull() }
            status = result
            if (result == null) message = "Unable to load sync status."
            loading = false
        }
    }

    androidx.compose.runtime.LaunchedEffect(baseUrl, token) { refresh() }
    LedgerCard(modifier = Modifier.fillMaxWidth().testTag("sync_diagnostics"), contentPadding = 16.dp) {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Text(
                    "Ledger sync",
                    style = androidx.compose.material3.MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.Bold,
                    modifier = Modifier.weight(1f).semantics { heading() },
                )
                OutlinedButton(onClick = ::refresh, enabled = !loading, modifier = Modifier.testTag("sync_refresh")) { Text("Refresh") }
            }
            if (loading) androidx.compose.material3.CircularProgressIndicator(modifier = Modifier.testTag("sync_loading"))
            status?.let { current ->
                Text("Pending: ${current.pendingCount}", modifier = Modifier.testTag("sync_pending_count"))
                Text("Failed: ${current.failedCount}", modifier = Modifier.testTag("sync_failed_count"))
                Text("Oldest pending: ${current.oldestPendingAt ?: "None"}", modifier = Modifier.testTag("sync_oldest_pending"))
                if (current.recentErrors.isNotEmpty()) {
                    Text("Recent errors", fontWeight = FontWeight.SemiBold)
                    current.recentErrors.take(MAX_DIAGNOSTIC_ITEMS).forEachIndexed { index, error ->
                        Text(
                            sanitizeSyncError(error.message),
                            color = Warning,
                            modifier = Modifier
                                .testTag("sync_error_$index")
                                .semantics { liveRegion = LiveRegionMode.Assertive },
                        )
                    }
                    val hiddenErrors = current.recentErrors.size - MAX_DIAGNOSTIC_ITEMS
                    if (hiddenErrors > 0) {
                        Text(
                            "$hiddenErrors more not shown",
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.testTag("sync_errors_truncated"),
                        )
                    }
                }
                Button(
                    onClick = {
                        scope.launch {
                            loading = true
                            val retried = withContext(Dispatchers.IO) { runCatching { LedgerApi(baseUrl, token).retrySync() }.getOrNull() }
                            message = if (retried == null) "Retry request failed." else "Retrying $retried failed item(s)."
                            loading = false
                            refresh()
                        }
                    },
                    enabled = current.failedCount > 0 && !loading,
                    modifier = Modifier.testTag("sync_retry_failed"),
                ) { Text("Retry failed") }
            }
            message?.let {
                Text(
                    it,
                    color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .testTag("sync_message")
                        .semantics { liveRegion = LiveRegionMode.Assertive },
                )
            }
        }
    }
}

@Composable
private fun CaptureRow(record: NotificationRecord, onRestore: () -> Unit) {
    val parseStatus = if (record.amountIdr != null && record.merchant != null && !record.reviewRequired) "Parsed" else "Needs review"
    val syncStatus = when (record.status) { "confirmed" -> "Confirmed locally"; "dismissed" -> "Dismissed"; else -> "Awaiting review" }
    LedgerCard(modifier = Modifier.fillMaxWidth().testTag("diagnostic_capture_${record.id}"), contentPadding = 14.dp) {
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(record.merchant ?: record.title, fontWeight = FontWeight.SemiBold)
            Text(record.body, color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant)
            Text("${record.bank} • $parseStatus • $syncStatus", color = if (record.status == "confirmed") Income else Warning)
            Text(record.packageName, color = androidx.compose.material3.MaterialTheme.colorScheme.onSurfaceVariant)
            if (record.status == "dismissed") {
                OutlinedButton(
                    onClick = onRestore,
                    modifier = Modifier.testTag("restore_capture_${record.id}"),
                ) { Text("Restore to inbox") }
            }
        }
    }
}
