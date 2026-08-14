@file:OptIn(
    androidx.compose.material3.ExperimentalMaterial3Api::class,
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class,
)

package com.afif.expensetracker.portfolio

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Add
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material.icons.rounded.Edit
import androidx.compose.material.icons.rounded.Refresh
import androidx.compose.material.icons.rounded.WarningAmber
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ElevatedAssistChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.data.PortfolioSnapshotCache
import com.afif.expensetracker.sync.LedgerApi
import com.afif.expensetracker.sync.LedgerAsset
import com.afif.expensetracker.sync.PortfolioSnapshot
import com.afif.expensetracker.ui.components.LedgerCard
import com.afif.expensetracker.ui.components.LedgerHeroCard
import com.afif.expensetracker.ui.components.LedgerMetricTile
import com.afif.expensetracker.ui.components.LedgerSectionHeader
import com.afif.expensetracker.ui.theme.Expense
import com.afif.expensetracker.ui.theme.Income
import com.afif.expensetracker.ui.theme.Ink
import com.afif.expensetracker.ui.theme.Warning
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject

private sealed interface PortfolioLoadState {
    data object Loading : PortfolioLoadState
    data class Loaded(val snapshot: PortfolioSnapshot, val fromCache: Boolean) : PortfolioLoadState
    data class Unavailable(val message: String) : PortfolioLoadState
}

@Composable
fun PortfolioOverview(onOpenAssets: () -> Unit) {
    val context = LocalContext.current
    // Read on composition so a connection saved in Settings is used when Dashboard is revisited.
    val settings = LedgerSettingsStore.read(context)
    val cache = remember { PortfolioSnapshotCache(context) }
    val scope = rememberCoroutineScope()
    var state by remember(settings.baseUrl) { mutableStateOf<PortfolioLoadState>(PortfolioLoadState.Loading) }
    var refreshing by remember(settings.baseUrl) { mutableStateOf(false) }

    suspend fun refresh() {
        if (settings.baseUrl.isBlank() || settings.token.isBlank()) {
            if (state !is PortfolioLoadState.Loaded) state = PortfolioLoadState.Unavailable("Sambungkan ledger untuk melihat posisi keuangan.")
            return
        }
        refreshing = true
        val fresh = withContext(Dispatchers.IO) { LedgerApi(settings.baseUrl, settings.token).portfolio() }
        if (fresh != null) {
            withContext(Dispatchers.IO) { cache.save(settings.baseUrl, fresh) }
            state = PortfolioLoadState.Loaded(fresh, fromCache = false)
        } else if (state !is PortfolioLoadState.Loaded) {
            state = PortfolioLoadState.Unavailable("Tidak dapat memperbarui posisi keuangan saat ini.")
        }
        refreshing = false
    }

    LaunchedEffect(settings.baseUrl, settings.token) {
        val cached = withContext(Dispatchers.IO) { cache.read(settings.baseUrl) }
        if (cached != null) state = PortfolioLoadState.Loaded(cached.snapshot, fromCache = true)
        refresh()
    }

    when (val current = state) {
        PortfolioLoadState.Loading -> LedgerCard(contentPadding = 18.dp) {
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
                Text("Memuat posisi keuangan…", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        is PortfolioLoadState.Unavailable -> LedgerCard(contentPadding = 18.dp) {
            LedgerSectionHeader("Posisi keuangan", current.message)
            OutlinedButton(onClick = { scope.launch { refresh() } }, enabled = !refreshing) {
                Text("Coba lagi")
            }
        }
        is PortfolioLoadState.Loaded -> PortfolioSnapshotContent(
            snapshot = current.snapshot,
            fromCache = current.fromCache,
            refreshing = refreshing,
            onRefresh = { scope.launch { refresh() } },
            onOpenAssets = onOpenAssets,
        )
    }
}

@Composable
private fun PortfolioSnapshotContent(
    snapshot: PortfolioSnapshot,
    fromCache: Boolean,
    refreshing: Boolean,
    onRefresh: () -> Unit,
    onOpenAssets: () -> Unit,
) {
    val freshness = portfolioFreshnessPresentation(snapshot.freshness, fromCache)
    LedgerHeroCard(modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text("NET WORTH", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = .78f))
                Text(formatIdr(snapshot.netWorthIdr), style = MaterialTheme.typography.displaySmall, color = MaterialTheme.colorScheme.onPrimaryContainer)
                Text("Kas dan aset setelah kewajiban", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = .76f))
            }
            ElevatedAssistChip(
                onClick = onRefresh,
                enabled = !refreshing,
                label = { Text(if (refreshing) "Memperbarui" else freshness.label) },
                leadingIcon = {
                    if (refreshing) CircularProgressIndicator(modifier = Modifier.size(14.dp), strokeWidth = 2.dp)
                    else Icon(if (freshness.isAttention) Icons.Rounded.WarningAmber else Icons.Rounded.Refresh, null, modifier = Modifier.size(16.dp))
                },
            )
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            LedgerMetricTile("Likuid", formatIdr(snapshot.totalLiquidIdr), Income, Modifier.weight(1f).widthIn(min = 132.dp))
            LedgerMetricTile("Kewajiban", formatIdr(snapshot.totalLiabilitiesIdr), Expense, Modifier.weight(1f).widthIn(min = 132.dp))
        }
        snapshot.asOf?.let { Text("Per $it", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = .7f)) }
    }
    LedgerSectionHeader("Saldo akun", "Saldo berasal dari ledger lengkap, bukan transaksi bulan ini.")
    val preferred = listOf("Cash", "Mandiri", "Jago", "BSI")
    FlowRow(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        preferred.forEach { name ->
            val account = dashboardAccountFor(snapshot.accounts, name)
            LedgerMetricTile(
                label = name,
                value = account?.let { formatIdr(it.balanceIdr) } ?: "Belum ada data",
                valueColor = account?.let { if (it.balanceIdr < 0) Expense else Income } ?: MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(1f).widthIn(min = 132.dp),
            )
        }
    }
    LedgerCard {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Aset & kewajiban", style = MaterialTheme.typography.titleMedium)
                Text("${snapshot.assets.count { !it.isLiability }} aset · ${snapshot.assets.count { it.isLiability }} kewajiban", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            TextButton(onClick = onOpenAssets) { Text("Kelola") }
        }
        snapshot.assets.take(3).forEach { asset ->
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                Column(Modifier.weight(1f)) {
                    Text(asset.name)
                    Text(if (asset.isLiability) "Kewajiban · ${asset.type}" else asset.type, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Text(asset.valueIdr?.let(::formatIdr) ?: "Belum dinilai", color = if (asset.valueIdr == null) Warning else MaterialTheme.colorScheme.onSurface)
            }
        }
        if (snapshot.assets.isEmpty()) Text("Belum ada aset atau kewajiban yang dicatat.", color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
    val notices = snapshot.warnings + snapshot.assets.filter { it.valueIdr == null && it.type.contains("gold", true) }.map { "${it.name} belum dinilai; tidak dihitung sebagai nol." }
    notices.distinct().forEach { warning ->
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.Top) {
            Icon(Icons.Rounded.WarningAmber, null, tint = Warning, modifier = Modifier.size(19.dp))
            Text(warning, color = Warning, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
fun AssetsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val settings = LedgerSettingsStore.read(context)
    val cache = remember { PortfolioSnapshotCache(context) }
    val scope = rememberCoroutineScope()
    var assets by remember { mutableStateOf<List<LedgerAsset>?>(null) }
    var busy by remember { mutableStateOf(false) }
    var message by remember { mutableStateOf<String?>(null) }
    var editing by remember { mutableStateOf<LedgerAsset?>(null) }
    var adding by remember { mutableStateOf(false) }
    var deleting by remember { mutableStateOf<LedgerAsset?>(null) }

    fun refresh() = scope.launch {
        if (settings.baseUrl.isBlank() || settings.token.isBlank()) { message = "Sambungkan ledger terlebih dahulu."; assets = emptyList(); return@launch }
        busy = true
        val result = withContext(Dispatchers.IO) { LedgerApi(settings.baseUrl, settings.token).assets() }
        assets = result
        if (result == null) message = "Tidak dapat memuat aset saat ini."
        busy = false
    }
    LaunchedEffect(Unit) { refresh() }

    Column(Modifier.fillMaxSize().background(Ink).verticalScroll(rememberScrollState()).padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Kembali") }
            Column(Modifier.weight(1f)) {
                Text("Aset & kewajiban", style = MaterialTheme.typography.headlineSmall, modifier = Modifier.semantics { heading() })
                Text("Nilai manual yang tersimpan di ledger", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            IconButton(onClick = ::refresh, enabled = !busy) { Icon(Icons.Rounded.Refresh, "Perbarui") }
        }
        Button(onClick = { adding = true }, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Icon(Icons.Rounded.Add, null); Spacer(Modifier.size(8.dp)); Text("Tambah aset atau kewajiban") }
        message?.let { Text(it, color = Warning, modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
        if (assets == null) Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) { CircularProgressIndicator(Modifier.size(22.dp), strokeWidth = 2.dp); Text("Memuat aset…") }
        else if (assets!!.isEmpty()) LedgerCard { Text("Belum ada aset atau kewajiban.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
        else assets!!.forEach { asset ->
            LedgerCard {
                Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
                    Column(Modifier.weight(1f)) {
                        Text(asset.name, style = MaterialTheme.typography.titleMedium)
                        Text("${if (asset.isLiability) "Kewajiban" else "Aset"} · ${asset.type} · ${asset.quantity} ${asset.unit}", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Text(asset.valueIdr?.let(::formatIdr) ?: "Belum dinilai", color = if (asset.valueIdr == null) Warning else if (asset.isLiability) Expense else Income)
                }
                asset.notes.takeIf(String::isNotBlank)?.let { Text(it, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant) }
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                    TextButton(onClick = { editing = asset }) { Icon(Icons.Rounded.Edit, null, Modifier.size(18.dp)); Text("Edit") }
                    TextButton(onClick = { deleting = asset }) { Icon(Icons.Rounded.DeleteOutline, null, Modifier.size(18.dp)); Text("Hapus") }
                }
            }
        }
    }
    if (adding || editing != null) AssetEditorDialog(
        asset = editing,
        busy = busy,
        onDismiss = { adding = false; editing = null },
        onSave = { draft -> scope.launch {
            when (val validation = validateAssetDraft(draft)) {
                is AssetValidationResult.Invalid -> message = validation.message
                is AssetValidationResult.Valid -> {
                    busy = true
                    val payload = validation.asset.toAssetPayload()
                    val api = LedgerApi(settings.baseUrl, settings.token)
                    val saved = withContext(Dispatchers.IO) { if (editing == null) api.createAsset(payload) else api.updateAsset(editing!!.id, payload) }
                    if (saved == null) message = api.lastError ?: "Aset tidak dapat disimpan."
                    else { adding = false; editing = null; refresh(); withContext(Dispatchers.IO) { api.portfolio()?.let { cache.save(settings.baseUrl, it) } } }
                    busy = false
                }
            }
        } },
    )
    deleting?.let { asset -> AlertDialog(
        onDismissRequest = { deleting = null }, title = { Text("Hapus ${asset.name}?") }, text = { Text("Aset ini akan dihapus dari ledger.") },
        confirmButton = { TextButton(onClick = { scope.launch { busy = true; val api = LedgerApi(settings.baseUrl, settings.token); val deleted = withContext(Dispatchers.IO) { api.deleteAsset(asset.id) }; if (deleted) { deleting = null; refresh(); withContext(Dispatchers.IO) { api.portfolio()?.let { cache.save(settings.baseUrl, it) } } } else message = api.lastError ?: "Aset tidak dapat dihapus."; busy = false } }) { Text("Hapus") } },
        dismissButton = { TextButton(onClick = { deleting = null }) { Text("Batal") } },
    ) }
}

/** Notion-backed account names include suffixes such as "Mandiri 1854" and "BSI 9400". */
internal fun dashboardAccountFor(
    accounts: List<com.afif.expensetracker.sync.PortfolioAccount>,
    displayLabel: String,
) = accounts.firstOrNull { it.name.equals(displayLabel, ignoreCase = true) }
    ?: accounts.firstOrNull {
        it.name.trim().startsWith("$displayLabel ", ignoreCase = true) ||
            it.name.contains(displayLabel, ignoreCase = true)
    }

internal fun ValidatedAssetDraft.toAssetPayload(): JSONObject = JSONObject()
    .put("name", name).put("type", type).put("value_idr", valueIdr ?: JSONObject.NULL).put("quantity", quantity)
    .put("unit", unit).put("last_updated", lastUpdated).put("notes", notes).put("is_liability", isLiability)

@Composable
private fun AssetEditorDialog(asset: LedgerAsset?, busy: Boolean, onDismiss: () -> Unit, onSave: (AssetDraft) -> Unit) {
    var name by remember(asset?.id) { mutableStateOf(asset?.name.orEmpty()) }
    var type by remember(asset?.id) { mutableStateOf(asset?.type ?: "Gold") }
    var value by remember(asset?.id) { mutableStateOf(asset?.valueIdr?.toString().orEmpty()) }
    var quantity by remember(asset?.id) { mutableStateOf(asset?.quantity?.toString() ?: "1") }
    var unit by remember(asset?.id) { mutableStateOf(asset?.unit ?: "unit") }
    var updated by remember(asset?.id) { mutableStateOf(asset?.lastUpdated ?: java.time.LocalDate.now().toString()) }
    var notes by remember(asset?.id) { mutableStateOf(asset?.notes.orEmpty()) }
    var liability by remember(asset?.id) { mutableStateOf(asset?.isLiability ?: false) }
    AlertDialog(
        onDismissRequest = { if (!busy) onDismiss() },
        title = { Text(if (asset == null) "Tambah posisi" else "Edit posisi") },
        text = { Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            OutlinedTextField(name, { name = it }, label = { Text("Nama") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(type, { type = it }, label = { Text("Jenis, mis. Gold atau Kredit") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(value, { value = it.filter(Char::isDigit) }, label = { Text("Nilai IDR (boleh kosong bila belum dinilai)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(quantity, { quantity = it }, label = { Text("Jumlah") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(unit, { unit = it }, label = { Text("Satuan") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(updated, { updated = it }, label = { Text("Tanggal pembaruan (YYYY-MM-DD)") }, singleLine = true, modifier = Modifier.fillMaxWidth())
            OutlinedTextField(notes, { notes = it }, label = { Text("Catatan (opsional)") }, modifier = Modifier.fillMaxWidth())
            Row(Modifier.fillMaxWidth().toggleable(value = liability, role = Role.Checkbox, onValueChange = { liability = it }), verticalAlignment = Alignment.CenterVertically) { androidx.compose.material3.Checkbox(checked = liability, onCheckedChange = null); Text("Ini kewajiban") }
        } },
        confirmButton = { TextButton(enabled = !busy, onClick = { onSave(AssetDraft(name, type, value, quantity, unit, updated, notes, liability)) }) { Text(if (busy) "Menyimpan…" else "Simpan") } },
        dismissButton = { TextButton(enabled = !busy, onClick = onDismiss) { Text("Batal") } },
    )
}
