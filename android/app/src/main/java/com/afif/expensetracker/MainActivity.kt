@file:OptIn(
    androidx.compose.foundation.layout.ExperimentalLayoutApi::class,
    androidx.compose.material3.ExperimentalMaterial3Api::class,
)

package com.afif.expensetracker

import android.content.Intent
import android.content.pm.ApplicationInfo
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.border
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.automirrored.rounded.ArrowForward
import androidx.compose.material.icons.automirrored.rounded.ReceiptLong
import androidx.compose.material.icons.rounded.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.navigation.compose.*
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.room.withTransaction
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.afif.expensetracker.data.*
import com.afif.expensetracker.budget.BudgetScreen
import com.afif.expensetracker.dashboard.DashboardSummaryCalculator
import com.afif.expensetracker.manual.ManualEntrySuggestions
import com.afif.expensetracker.manual.ManualTransactionDraft
import com.afif.expensetracker.manual.ManualTransactionDialog
import com.afif.expensetracker.manual.ManualTransactionKind
import com.afif.expensetracker.manual.ManualTransactionStore
import com.afif.expensetracker.manual.PendingManualMutationResult
import com.afif.expensetracker.settings.SettingsValidationResult
import com.afif.expensetracker.settings.validateSettings
import com.afif.expensetracker.sync.SyncScheduler
import com.afif.expensetracker.sync.LedgerApi
import com.afif.expensetracker.portfolio.AssetsScreen
import com.afif.expensetracker.portfolio.PortfolioOverview
import com.afif.expensetracker.portfolio.formatIdr
import com.afif.expensetracker.transactions.TransactionKind
import com.afif.expensetracker.transactions.filterTransactions
import com.afif.expensetracker.transactions.groupTransactions
import com.afif.expensetracker.ui.components.LedgerCard
import com.afif.expensetracker.ui.components.LedgerHeroCard
import com.afif.expensetracker.ui.components.LedgerMetricTile
import com.afif.expensetracker.ui.components.LedgerSectionHeader
import com.afif.expensetracker.ui.components.LedgerSpacing
import com.afif.expensetracker.ui.components.LedgerDateField
import com.afif.expensetracker.ui.components.LedgerDatePickerDialog
import com.afif.expensetracker.ui.components.LedgerIdrAmountField
import com.afif.expensetracker.ui.theme.*
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.text.NumberFormat
import java.time.Instant
import java.time.LocalDate
import java.time.YearMonth
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.*

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        SyncScheduler.schedulePeriodic(this)
        setContent {
            var palette by remember { mutableStateOf(LedgerThemePreferences.read(this)) }
            LedgerTheme(palette) {
                LedgerApp(
                    selectedPalette = palette,
                    onPaletteSelected = { selected ->
                        LedgerThemePreferences.save(this, selected)
                        palette = selected
                    },
                )
            }
        }
    }
}

private fun money(minor: Long): String = formatIdr(minor)

@Composable
internal fun rememberDashboardMonth(
    currentMonth: () -> YearMonth = { YearMonth.now() },
): YearMonth {
    var month by remember { mutableStateOf(currentMonth()) }
    val currentMonthProvider by rememberUpdatedState(currentMonth)
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) month = currentMonthProvider()
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    return month
}

private data class TopLevelDestination(
    val route: String,
    val label: String,
    val icon: ImageVector,
)

@Composable
private fun LedgerApp(
    selectedPalette: LedgerThemePalette,
    onPaletteSelected: (LedgerThemePalette) -> Unit,
) {
    val nav = rememberNavController()
    val topLevelRoutes = listOf(
        TopLevelDestination("dashboard", "Dashboard", Icons.Rounded.Home),
        TopLevelDestination("inbox", "Inbox", Icons.Rounded.Inbox),
        TopLevelDestination("transactions", "History", Icons.AutoMirrored.Rounded.ReceiptLong),
        TopLevelDestination("budgets", "Budgets", Icons.Rounded.AccountBalanceWallet),
        TopLevelDestination("settings", "Settings", Icons.Rounded.Settings),
    )
    val backStackEntry by nav.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route
    val showNavigationBar = topLevelRoutes.any { it.route == currentRoute }

    Scaffold(
        bottomBar = {
            if (showNavigationBar) {
                LedgerBottomNavigation(
                    destinations = topLevelRoutes,
                    selectedRoute = currentRoute,
                    onSelect = { destination ->
                        nav.navigate(destination.route) {
                            popUpTo(nav.graph.findStartDestination().id) { saveState = true }
                            launchSingleTop = true
                            restoreState = true
                        }
                    },
                )
            }
        },
    ) { padding ->
        NavHost(
            navController = nav,
            startDestination = "dashboard",
            modifier = Modifier.padding(padding),
        ) {
            composable("dashboard") {
                Dashboard(
                    openInbox = { nav.navigate("inbox") },
                    openAssets = { nav.navigate("assets") },
                )
            }
            composable("inbox") {
                Inbox()
            }
            composable("transactions") {
                Transactions { nav.navigate("transaction/${it.id}") }
            }
            composable("transaction/{id}") { entry ->
                TransactionDetail(entry.arguments?.getString("id").orEmpty()) {
                    nav.popBackStack()
                }
            }
            composable("budgets") {
                BudgetScreen(onOpenSettings = { nav.navigate("settings") })
            }
            composable("settings") {
                SettingsScreen(
                    selectedPalette = selectedPalette,
                    onPaletteSelected = onPaletteSelected,
                    openDiagnostics = { nav.navigate("diagnostics") },
                )
            }
            composable("diagnostics") {
                DiagnosticsScreen(onBack = { nav.popBackStack() })
            }
            composable("assets") {
                AssetsScreen(onBack = { nav.popBackStack() })
            }
        }
    }
}

@Composable
private fun LedgerBottomNavigation(
    destinations: List<TopLevelDestination>,
    selectedRoute: String?,
    onSelect: (TopLevelDestination) -> Unit,
) {
    Surface(
        color = MaterialTheme.colorScheme.surfaceContainerHigh,
        shape = RoundedCornerShape(topStart = 28.dp, topEnd = 28.dp),
        tonalElevation = 8.dp,
        shadowElevation = 18.dp,
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .navigationBarsPadding()
                .padding(horizontal = 8.dp, vertical = 7.dp)
                .selectableGroup(),
            horizontalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            destinations.forEach { destination ->
                val selected = destination.route == selectedRoute
                Column(
                    modifier = Modifier
                        .weight(1f)
                        .heightIn(min = 58.dp)
                        .selectable(
                            selected = selected,
                            onClick = { onSelect(destination) },
                            role = Role.Tab,
                        )
                        .testTag("nav_${destination.route}")
                        .semantics(mergeDescendants = true) {
                            contentDescription = destination.label
                        }
                        .padding(vertical = 4.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(3.dp, Alignment.CenterVertically),
                ) {
                    Surface(
                        color = if (selected) MaterialTheme.colorScheme.primaryContainer else Color.Transparent,
                        shape = RoundedCornerShape(14.dp),
                    ) {
                        Icon(
                            destination.icon,
                            contentDescription = null,
                            tint = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp).size(22.dp),
                        )
                    }
                    Text(
                        destination.label,
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
                        color = if (selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}

@Composable
private fun Dashboard(openInbox: () -> Unit, openAssets: () -> Unit) {
    val db = LedgerDatabase.get(LocalContext.current)
    val month = rememberDashboardMonth()
    val monthStart = remember(month) { month.atDay(1).atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli() }
    val monthEnd = remember(month) { month.plusMonths(1).atDay(1).atStartOfDay(ZoneId.systemDefault()).toInstant().toEpochMilli() }
    val transactionsFlow = remember(db, monthStart, monthEnd) {
        db.transactionDao().observeOccurredBetween(monthStart, monthEnd)
    }
    val transactionsState by transactionsFlow.collectAsState(initial = null)
    val transactions = transactionsState.orEmpty()
    val summary = remember(transactions, month) {
        DashboardSummaryCalculator.summarize(transactions, month)
    }
    val monthLabel = remember(month) {
        month.format(DateTimeFormatter.ofPattern("MMMM yyyy", Locale.ENGLISH))
            .replaceFirstChar { it.uppercase() }
    }

    LazyColumn(
        Modifier.fillMaxSize().background(Ink).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(18.dp),
    ) {
        item {
            Text(
                "Overview",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )
            Text(
                "$monthLabel · your local-first ledger",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        item {
            PortfolioOverview(onOpenAssets = openAssets)
        }
        if (transactionsState == null) {
            item {
                LedgerCard(
                    modifier = Modifier.testTag("dashboard_loading"),
                    contentPadding = 22.dp,
                ) {
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                        Text("Loading your ledger…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        } else {
            item {
                LedgerHeroCard(modifier = Modifier.fillMaxWidth()) {
                    Text(
                        "MONTHLY SPEND",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.78f),
                    )
                    Text(
                        money(summary.totalExpenseMinor),
                        style = MaterialTheme.typography.displaySmall,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onPrimaryContainer,
                        modifier = Modifier.testTag("dashboard_month_expense"),
                    )
                    FlowRow(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                        verticalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        LedgerMetricTile(
                            label = "Income",
                            value = money(summary.totalIncomeMinor),
                            valueColor = Income,
                            modifier = Modifier.weight(1f).widthIn(min = 120.dp),
                        )
                        LedgerMetricTile(
                            label = "Net flow",
                            value = money(summary.netMinor),
                            valueColor = if (summary.netMinor >= 0) Income else Expense,
                            modifier = Modifier.weight(1f).widthIn(min = 120.dp),
                        )
                    }
                    Text(
                        "${summary.transactionCount} confirmed transactions",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer.copy(alpha = 0.72f),
                    )
                }
            }
            if (summary.topExpenseCategories.isNotEmpty()) {
                item {
                    Text(
                        "Top categories",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.semantics { heading() },
                    )
                }
                items(
                    summary.topExpenseCategories,
                    key = { "dashboard_category_${it.category}" },
                ) { category ->
                    LedgerCard(contentPadding = 14.dp) {
                        Row(
                            Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                        ) {
                            Text(category.category, fontWeight = FontWeight.SemiBold)
                            Text(
                                money(category.amountMinor),
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        LinearProgressIndicator(
                            progress = {
                                (
                                    category.amountMinor.toDouble() /
                                        summary.totalExpenseMinor.coerceAtLeast(1)
                                    ).coerceIn(0.0, 1.0).toFloat()
                            },
                            modifier = Modifier.fillMaxWidth(),
                            color = ChartAccent,
                            trackColor = MaterialTheme.colorScheme.surfaceContainerHighest,
                        )
                    }
                }
            }
            item {
                FlowRow(
                    Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    Text(
                        "Recent this month",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier
                            .weight(1f)
                            .widthIn(min = 160.dp)
                            .semantics { heading() },
                    )
                    TextButton(onClick = openInbox) {
                        Text("Review inbox")
                        Icon(Icons.AutoMirrored.Rounded.ArrowForward, null)
                    }
                }
            }
            if (summary.recentTransactions.isEmpty()) {
                item {
                    Text(
                        "No confirmed transactions this month.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else {
                items(summary.recentTransactions, key = { it.id }) {
                    TransactionRow(it)
                }
            }
        }
    }
}

@Composable
private fun Inbox() {
    val context = LocalContext.current
    val db = LedgerDatabase.get(context)
    val recordsFlow = remember(db) { db.notificationDao().observeByStatus("pending", 100) }
    val recordsState by recordsFlow.collectAsState(initial = null)
    val records = recordsState.orEmpty()
    val scope = rememberCoroutineScope()
    val pending = records
    var reviewingId by rememberSaveable { mutableStateOf<Long?>(null) }
    var dismissing by remember { mutableStateOf<NotificationRecord?>(null) }
    val reviewing = reviewingId?.let { id -> pending.firstOrNull { it.id == id } }
    var reviewSaving by rememberSaveable { mutableStateOf(false) }
    var reviewError by rememberSaveable { mutableStateOf<String?>(null) }

    LazyColumn(
        Modifier.fillMaxSize().background(Ink).padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Text(
                "Review inbox",
                style = MaterialTheme.typography.headlineMedium,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.semantics { heading() },
            )
            Text("Captured notifications stay on-device until you confirm.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        if (recordsState == null) {
            item {
                Row(
                    Modifier.fillMaxWidth().testTag("inbox_loading"),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                    Text("Loading captured notifications…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
        } else if (pending.isEmpty()) item {
            Text("Inbox is clear.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        } else items(pending, key = { it.id }) { rec ->
            LedgerCard(
                modifier = Modifier.testTag("inbox_item_${rec.sourceRef}"),
                contentPadding = 16.dp,
            ) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        Surface(shape = CircleShape, color = MaterialTheme.colorScheme.primaryContainer) {
                            Icon(
                                Icons.Rounded.Notifications,
                                contentDescription = null,
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.padding(8.dp).size(18.dp),
                            )
                        }
                        Text(rec.merchant ?: rec.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    }
                    Text(
                        rec.body,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 3,
                        overflow = TextOverflow.Ellipsis,
                    )
                    if (rec.reviewRequired) {
                        Text("Check the detected details before saving", color = Warning)
                    }
                    if (rec.suspectedDuplicateOf != null) {
                        Text(
                            "Possible repost of an earlier capture. Keep it only if this was a separate purchase.",
                            color = Warning,
                            modifier = Modifier.testTag("suspected_repost_${rec.sourceRef}"),
                        )
                    }
                    FlowRow(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.End,
                        verticalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        TextButton(onClick = { dismissing = rec }) {
                            Text(if (rec.suspectedDuplicateOf != null) "Discard repost" else "Dismiss")
                        }
                        Button(
                            modifier = Modifier.testTag("confirm_${rec.sourceRef}"),
                            enabled = rec.reviewRequired || (rec.amountIdr ?: 0L) > 0,
                            onClick = {
                                if (rec.reviewRequired) {
                                    reviewingId = rec.id
                                    reviewError = null
                                } else {
                                    scope.launch {
                                        if (NotificationConfirmationStore(db).confirm(rec.id)) {
                                            SyncScheduler.enqueue(context)
                                        }
                                    }
                                }
                            },
                        ) {
                            Text(if (rec.reviewRequired) "Review capture" else "Add expense")
                        }
                    }
            }
        }
    }
    reviewing?.let { record ->
        NotificationReviewDialog(
            record = record,
            saving = reviewSaving,
            externalError = reviewError,
            onDismiss = {
                if (!reviewSaving) {
                    reviewingId = null
                    reviewError = null
                }
            },
            onConfirm = { draft ->
                scope.launch {
                    reviewSaving = true
                    reviewError = null
                    try {
                        val confirmed = NotificationConfirmationStore(db).confirm(record.id, draft)
                        if (confirmed) {
                            reviewingId = null
                            SyncScheduler.enqueue(context)
                        } else {
                            reviewError = "This capture changed or could not be saved. Review it again."
                        }
                    } catch (error: Exception) {
                        reviewError = "Could not save this capture. Try again."
                    } finally {
                        reviewSaving = false
                    }
                }
            },
        )
    }
    dismissing?.let { record ->
        AlertDialog(
            onDismissRequest = { dismissing = null },
            title = { Text(if (record.suspectedDuplicateOf != null) "Discard repost?" else "Dismiss capture?") },
            text = { Text("You can restore dismissed captures later from Notification diagnostics.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        dismissing = null
                        scope.launch { db.notificationDao().updateStatus(record.id, "dismissed") }
                    },
                    modifier = Modifier.testTag("dismiss_confirm_${record.sourceRef}"),
                ) { Text("Dismiss") }
            },
            dismissButton = {
                TextButton(onClick = { dismissing = null }) { Text("Cancel") }
            },
        )
    }
}

@Composable
private fun NotificationReviewDialog(
    record: NotificationRecord,
    saving: Boolean,
    externalError: String?,
    onDismiss: () -> Unit,
    onConfirm: (NotificationConfirmationDraft) -> Unit,
) {
    var merchant by rememberSaveable(record.id) {
        mutableStateOf(record.merchant ?: record.title)
    }
    var amount by rememberSaveable(record.id) {
        mutableStateOf(record.amountIdr?.toString().orEmpty())
    }
    var occurredOn by rememberSaveable(record.id) {
        mutableStateOf(
            record.occurredOn ?: Instant.ofEpochMilli(record.receivedAt)
                .atZone(ZoneId.systemDefault()).toLocalDate().toString(),
        )
    }
    var description by rememberSaveable(record.id) { mutableStateOf(record.title) }
    var category by rememberSaveable(record.id) { mutableStateOf("Uncategorized") }
    var account by rememberSaveable(record.id) {
        mutableStateOf(
            when (record.bank) {
                "BSI" -> "BSI"
                "LIVIN_MANDIRI" -> "Mandiri"
                "JAGO" -> "Jago"
                else -> ""
            },
        )
    }
    var accountMenuExpanded by rememberSaveable(record.id) { mutableStateOf(false) }
    var kind by rememberSaveable(record.id) {
        mutableStateOf(if (record.direction == "CREDIT") "income" else "expense")
    }
    var selfTransfer by rememberSaveable(record.id) { mutableStateOf(false) }
    var validationError by rememberSaveable(record.id) { mutableStateOf<String?>(null) }
    var showDatePicker by rememberSaveable(record.id) { mutableStateOf(false) }

    val scrollState = rememberScrollState()
    val canonicalAccounts = listOf("BSI", "Mandiri", "Jago", "Cash")

    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .fillMaxHeight(0.92f)
                .padding(horizontal = 20.dp)
                .imePadding()
                .testTag("review_dialog"),
            shape = MaterialTheme.shapes.extraLarge,
            color = MaterialTheme.colorScheme.surfaceContainer,
            tonalElevation = 8.dp,
            shadowElevation = 12.dp,
        ) {
            Column(Modifier.fillMaxSize()) {
                Row(
                    modifier = Modifier.fillMaxWidth().padding(horizontal = 24.dp, vertical = 18.dp),
                    horizontalArrangement = Arrangement.spacedBy(14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Surface(
                        shape = MaterialTheme.shapes.medium,
                        color = MaterialTheme.colorScheme.primaryContainer,
                    ) {
                        Icon(
                            Icons.AutoMirrored.Rounded.ReceiptLong,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.padding(11.dp).size(24.dp),
                        )
                    }
                    Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                        Text(
                            "Review transaction",
                            style = MaterialTheme.typography.headlineSmall,
                            fontWeight = FontWeight.Bold,
                            modifier = Modifier.semantics { heading() },
                        )
                        Text(
                            "Verify the capture before it enters your ledger.",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                Box(
                    Modifier
                        .fillMaxWidth()
                        .weight(1f)
                        .background(MaterialTheme.colorScheme.surfaceContainerLow),
                ) {
                    Column(
                        Modifier
                            .fillMaxSize()
                            .verticalScroll(scrollState)
                            .padding(horizontal = 24.dp, vertical = 18.dp)
                            .padding(bottom = 32.dp)
                            .testTag("review_scroll_content"),
                        verticalArrangement = Arrangement.spacedBy(14.dp),
                    ) {
                Text(
                    "Captured notification",
                    style = MaterialTheme.typography.labelLarge,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (record.suspectedDuplicateOf != null) {
                    Text(
                        "Possible repost: confirm only if this was a separate purchase; otherwise cancel and discard it from the inbox.",
                        color = Warning,
                        modifier = Modifier.testTag("review_suspected_repost"),
                    )
                }
                Text(
                    record.body,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                OutlinedTextField(
                    merchant,
                    { merchant = it },
                    label = { Text("Merchant") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().testTag("review_merchant"),
                )
                LedgerIdrAmountField(
                    amount,
                    { amount = it },
                    modifier = Modifier.fillMaxWidth(),
                    testTag = "review_amount",
                )
                Column(verticalArrangement = Arrangement.spacedBy(9.dp)) {
                    Text("Transaction type", style = MaterialTheme.typography.labelLarge)
                    Row(
                        Modifier.fillMaxWidth().selectableGroup(),
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        ReviewKindChoice(
                            label = "Expense",
                            icon = Icons.Rounded.SouthEast,
                            selected = kind == "expense",
                            accent = Expense,
                            tag = "review_kind_expense",
                            onClick = { kind = "expense" },
                            modifier = Modifier.weight(1f),
                        )
                        ReviewKindChoice(
                            label = "Income",
                            icon = Icons.Rounded.NorthEast,
                            selected = kind == "income",
                            accent = Income,
                            tag = "review_kind_income",
                            onClick = { kind = "income" },
                            modifier = Modifier.weight(1f),
                        )
                    }
                }
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("review_self_transfer")
                        .toggleable(
                            value = selfTransfer,
                            role = Role.Checkbox,
                            onValueChange = { selfTransfer = it },
                        ),
                    verticalAlignment = Alignment.Top,
                ) {
                    Checkbox(
                        checked = selfTransfer,
                        onCheckedChange = null,
                    )
                    Column(Modifier.padding(top = 10.dp)) {
                        Text(
                            "Own-account transfer",
                            style = MaterialTheme.typography.labelLarge,
                        )
                        Text(
                            "Use this only when the money moved between your own accounts. The email watcher will balance both legs; only any admin fee counts as spending.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                LedgerDateField(
                    occurredOn,
                    { occurredOn = it },
                    { showDatePicker = true },
                    modifier = Modifier.fillMaxWidth(),
                    testTag = "review_date",
                )
                OutlinedTextField(
                    description,
                    { description = it },
                    label = { Text("Description") },
                    modifier = Modifier.fillMaxWidth().testTag("review_description"),
                )
                OutlinedTextField(
                    category,
                    { category = it },
                    label = { Text("Category") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth().testTag("review_category"),
                )
                ExposedDropdownMenuBox(
                    expanded = accountMenuExpanded,
                    onExpandedChange = { accountMenuExpanded = !accountMenuExpanded },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    OutlinedTextField(
                        value = account,
                        onValueChange = {},
                        readOnly = true,
                        label = { Text("Account") },
                        trailingIcon = {
                            ExposedDropdownMenuDefaults.TrailingIcon(expanded = accountMenuExpanded)
                        },
                        leadingIcon = { Icon(Icons.Rounded.AccountBalanceWallet, contentDescription = null) },
                        modifier = Modifier
                            .fillMaxWidth()
                            .menuAnchor()
                            .testTag("review_account"),
                    )
                    ExposedDropdownMenu(
                        expanded = accountMenuExpanded,
                        onDismissRequest = { accountMenuExpanded = false },
                    ) {
                        canonicalAccounts.forEach { option ->
                            DropdownMenuItem(
                                text = { Text(option) },
                                onClick = {
                                    account = option
                                    accountMenuExpanded = false
                                },
                                leadingIcon = if (account == option) {
                                    { Icon(Icons.Rounded.Check, contentDescription = null) }
                                } else null,
                                modifier = Modifier.testTag(
                                    "review_account_${option.lowercase(Locale.ROOT)}",
                                ),
                            )
                        }
                    }
                }
                (validationError ?: externalError)?.let {
                    Text(
                        it,
                        color = Expense,
                        modifier = Modifier
                            .testTag("review_error")
                            .semantics { liveRegion = LiveRegionMode.Assertive },
                    )
                }
                    }
                    if (scrollState.value > 0) {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(18.dp)
                                .align(Alignment.TopCenter)
                                .background(
                                    Brush.verticalGradient(
                                        listOf(MaterialTheme.colorScheme.surfaceContainerLow, Color.Transparent),
                                    ),
                                )
                                .testTag("review_top_scroll_cue"),
                        )
                    }
                    if (scrollState.value < scrollState.maxValue) {
                        Box(
                            Modifier
                                .fillMaxWidth()
                                .height(22.dp)
                                .align(Alignment.BottomCenter)
                                .background(
                                    Brush.verticalGradient(
                                        listOf(Color.Transparent, MaterialTheme.colorScheme.surfaceContainerLow),
                                    ),
                                )
                                .testTag("review_bottom_scroll_cue"),
                        )
                    }
                }
                HorizontalDivider(color = MaterialTheme.colorScheme.primary.copy(alpha = 0.42f))
                Surface(
                    color = MaterialTheme.colorScheme.surfaceContainerHighest,
                    tonalElevation = 10.dp,
                    modifier = Modifier
                        .fillMaxWidth()
                        .shadow(8.dp)
                        .testTag("review_sticky_footer"),
                ) {
                    FlowRow(
                        modifier = Modifier
                            .fillMaxWidth()
                            .navigationBarsPadding()
                            .padding(horizontal = 20.dp, vertical = 14.dp),
                        horizontalArrangement = Arrangement.End,
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        TextButton(onClick = onDismiss, enabled = !saving) {
                            Text("Cancel")
                        }
                        Button(
                            enabled = !saving,
                            modifier = Modifier.testTag("review_save"),
                            onClick = {
                                val amountIdr = amount.toLongOrNull()
                                val validDate = runCatching { LocalDate.parse(occurredOn) }.isSuccess
                                if (
                                    merchant.isBlank() || amountIdr == null || amountIdr <= 0 ||
                                    !validDate || description.isBlank() || category.isBlank() ||
                                    account.isBlank()
                                ) {
                                    validationError =
                                        "Enter a merchant, positive amount, ISO date, description, category, and account."
                                } else {
                                    validationError = null
                                    onConfirm(
                                        NotificationConfirmationDraft(
                                            merchant = merchant.trim(),
                                            amountIdr = amountIdr,
                                            occurredOn = occurredOn,
                                            description = description.trim(),
                                            category = category.trim(),
                                            account = account.trim(),
                                            kind = kind,
                                            selfTransfer = selfTransfer,
                                        ),
                                    )
                                }
                            },
                        ) {
                            Text(if (saving) "Saving…" else "Save transaction")
                        }
                    }
                }
            }
        }
    }
    if (showDatePicker) {
        LedgerDatePickerDialog(
            currentValue = occurredOn,
            onDateSelected = { occurredOn = it; validationError = null },
            onDismiss = { showDatePicker = false },
            testTag = "review_date",
        )
    }
}

@Composable
private fun ReviewKindChoice(
    label: String,
    icon: ImageVector,
    selected: Boolean,
    accent: Color,
    tag: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Surface(
        modifier = modifier
            .heightIn(min = 56.dp)
            .selectable(selected = selected, onClick = onClick, role = Role.RadioButton)
            .testTag(tag)
            .semantics(mergeDescendants = true) { contentDescription = "$label transaction type" },
        shape = MaterialTheme.shapes.medium,
        color = if (selected) accent.copy(alpha = 0.16f)
        else MaterialTheme.colorScheme.surfaceContainerHigh,
        border = BorderStroke(
            if (selected) 1.5.dp else 1.dp,
            if (selected) accent else MaterialTheme.colorScheme.outlineVariant,
        ),
    ) {
        Row(
            modifier = Modifier.padding(horizontal = 14.dp, vertical = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(9.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Icon(icon, contentDescription = null, tint = accent, modifier = Modifier.size(20.dp))
            Text(label, style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun Transactions(openDetail: (TransactionEntity) -> Unit) {
    val context = LocalContext.current
    val db = LedgerDatabase.get(context)
    val transactionsFlow = remember(db) { db.transactionDao().observeRecent(500) }
    val transactionsState by transactionsFlow.collectAsState(initial = null)
    val transactions = transactionsState.orEmpty()
    var query by rememberSaveable { mutableStateOf("") }
    var kindName by rememberSaveable { mutableStateOf(TransactionKind.ALL.name) }
    val kind = remember(kindName) { TransactionKind.valueOf(kindName) }
    var syncMessage by remember { mutableStateOf<String?>(null) }
    var showManualEntry by rememberSaveable { mutableStateOf(false) }
    var manualSaving by remember { mutableStateOf(false) }
    var manualError by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val filtered = remember(transactions, query, kind) {
        filterTransactions(transactions, query = query, kind = kind)
    }
    val groups = remember(filtered) { groupTransactions(filtered) }
    val manualSuggestions = remember(transactions) {
        ManualEntrySuggestions.build(transactions)
    }

    Box(Modifier.fillMaxSize().background(Ink)) {
        LazyColumn(
            Modifier.fillMaxSize().padding(horizontal = 20.dp),
            contentPadding = PaddingValues(top = 20.dp, bottom = 104.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
        item {
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                Column {
                    Text(
                        "Transactions",
                        style = MaterialTheme.typography.headlineMedium,
                        fontWeight = FontWeight.Bold,
                        modifier = Modifier.semantics { heading() },
                    )
                    Text(
                        if (transactionsState == null) "Loading local ledger…"
                        else "${transactions.size} in your local ledger",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                TextButton(
                    onClick = {
                        SyncScheduler.enqueue(context)
                        syncMessage = "Sync queued. New server changes will appear automatically."
                    },
                    modifier = Modifier.testTag("transaction_refresh"),
                ) {
                    Text("Sync now")
                }
            }
        }
        syncMessage?.let { message ->
            item {
                Text(
                    message,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .testTag("transaction_sync_message")
                        .semantics { liveRegion = LiveRegionMode.Polite },
                )
            }
        }
        item {
            OutlinedTextField(
                value = query,
                onValueChange = { query = it },
                modifier = Modifier.fillMaxWidth().testTag("transaction_search"),
                singleLine = true,
                label = { Text("Search transactions") },
                placeholder = { Text("Merchant, category, account…") },
                leadingIcon = { Icon(Icons.Rounded.Search, null) },
                trailingIcon = {
                    if (query.isNotEmpty()) {
                        IconButton(
                            onClick = { query = "" },
                            modifier = Modifier.testTag("transaction_search_clear"),
                        ) {
                            Icon(Icons.Rounded.Close, "Clear search")
                        }
                    }
                },
            )
        }
        item {
            FlowRow(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                TransactionKind.entries.forEach { option ->
                    FilterChip(
                        selected = kind == option,
                        onClick = { kindName = option.name },
                        label = {
                            Text(
                                when (option) {
                                    TransactionKind.ALL -> "All"
                                    TransactionKind.EXPENSE -> "Expenses"
                                    TransactionKind.INCOME -> "Income"
                                },
                            )
                        },
                        modifier = Modifier.testTag(
                            "transaction_filter_${option.name.lowercase(Locale.ROOT)}",
                        ),
                    )
                }
            }
        }
            if (transactionsState == null) {
                item {
                    Row(
                        Modifier.fillMaxWidth().testTag("transactions_loading"),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                        Text("Loading transactions…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            } else if (transactions.isEmpty()) {
                item {
                    Text(
                        "No transactions yet.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            } else if (filtered.isEmpty()) {
                item {
                    Text(
                        "No transactions match these filters.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp).testTag("transaction_empty_filter"),
                    )
                }
            } else {
                groups.forEach { group ->
                    item(key = "transaction_group_${group.key}") {
                        Text(
                            LocalDate.parse(group.key).format(
                                DateTimeFormatter.ofPattern("EEE, d MMM yyyy", Locale.ENGLISH),
                            ),
                            style = MaterialTheme.typography.labelLarge,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.padding(top = 8.dp),
                        )
                    }
                    items(group.transactions, key = { it.id }) { transaction ->
                        TransactionRow(transaction, onClick = { openDetail(transaction) })
                    }
                }
            }
        }
        ExtendedFloatingActionButton(
            onClick = {
                manualError = null
                showManualEntry = true
            },
            icon = { Icon(Icons.Rounded.Add, null) },
            text = { Text("Add transaction") },
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .padding(20.dp)
                .testTag("transaction_add"),
        )
    }
    if (showManualEntry) {
        ManualTransactionDialog(
            saving = manualSaving,
            externalError = manualError,
            categorySuggestions = manualSuggestions.categories,
            accountSuggestions = manualSuggestions.accounts,
            onDismiss = {
                if (!manualSaving) {
                    showManualEntry = false
                    manualError = null
                }
            },
            onConfirm = { draft ->
                if (!manualSaving) {
                    manualSaving = true
                    manualError = null
                    scope.launch {
                        val createdId = runCatching {
                            ManualTransactionStore(db).create(draft)
                        }.getOrNull()
                        manualSaving = false
                        if (createdId != null) {
                            showManualEntry = false
                            syncMessage = "Transaction saved locally. Sync queued."
                            SyncScheduler.enqueue(context)
                        } else {
                            manualError = "These transaction details could not be saved."
                        }
                    }
                }
            },
        )
    }
}

@Composable
private fun SettingsScreen(
    selectedPalette: LedgerThemePalette,
    onPaletteSelected: (LedgerThemePalette) -> Unit,
    openDiagnostics: () -> Unit,
) {
    val context = LocalContext.current
    val settings = remember { LedgerSettingsStore.read(context) }
    val allowCleartext = (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE) != 0
    var baseUrl by rememberSaveable { mutableStateOf(settings.baseUrl) }
    var token by rememberSaveable { mutableStateOf(settings.token) }
    var tokenVisible by rememberSaveable { mutableStateOf(false) }
    var message by rememberSaveable { mutableStateOf<String?>(null) }
    var messageIsError by rememberSaveable { mutableStateOf(false) }
    var testingConnection by rememberSaveable { mutableStateOf(false) }
    val scope = rememberCoroutineScope()
    Column(
        Modifier
            .fillMaxSize()
            .background(Ink)
            .verticalScroll(rememberScrollState())
            .imePadding()
            .padding(LedgerSpacing.Screen),
        verticalArrangement = Arrangement.spacedBy(LedgerSpacing.Section),
    ) {
        LedgerSectionHeader(
            title = "Settings",
            subtitle = "Connect your private ledger and manage on-device capture.",
        )
        LedgerCard {
            LedgerSectionHeader(
                title = "Appearance",
                subtitle = "Choose a polished palette. Changes apply immediately and stay selected on this device.",
            )
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                LedgerThemePalette.entries.forEach { palette ->
                    val selected = selectedPalette == palette
                    Surface(
                        modifier = Modifier
                            .weight(1f)
                            .heightIn(min = 82.dp)
                            .selectable(
                                selected = selected,
                                onClick = { onPaletteSelected(palette) },
                                role = Role.RadioButton,
                            )
                            .testTag("theme_${palette.storageValue}")
                            .semantics(mergeDescendants = true) {
                                contentDescription = "${palette.label} theme"
                            },
                        shape = MaterialTheme.shapes.medium,
                        color = if (selected) MaterialTheme.colorScheme.primaryContainer
                        else MaterialTheme.colorScheme.surfaceContainerHigh,
                        border = BorderStroke(
                            if (selected) 1.5.dp else 1.dp,
                            if (selected) MaterialTheme.colorScheme.primary
                            else MaterialTheme.colorScheme.outlineVariant,
                        ),
                    ) {
                        Column(
                            modifier = Modifier.padding(horizontal = 8.dp, vertical = 10.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                            verticalArrangement = Arrangement.spacedBy(7.dp),
                        ) {
                            Row(horizontalArrangement = Arrangement.spacedBy((-5).dp)) {
                                Box(Modifier.size(22.dp).background(palette.previewPrimary, CircleShape))
                                Box(
                                    Modifier
                                        .size(22.dp)
                                        .background(palette.previewSecondary, CircleShape)
                                        .border(2.dp, MaterialTheme.colorScheme.surfaceContainerHigh, CircleShape),
                                )
                            }
                            Text(
                                palette.label,
                                style = MaterialTheme.typography.labelMedium,
                                fontWeight = if (selected) FontWeight.Bold else FontWeight.Medium,
                                color = if (selected) MaterialTheme.colorScheme.onPrimaryContainer
                                else MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 2,
                            )
                        }
                    }
                }
            }
        }
        LedgerCard {
            LedgerSectionHeader(
                title = "Server connection",
                subtitle = if (allowCleartext) "HTTPS is required in release builds; HTTP is available only for local debug testing." else "Use an HTTPS endpoint for your private ledger.",
            )
            message?.let {
                Text(
                    it,
                    color = if (messageIsError) Expense
                    else MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .fillMaxWidth()
                        .testTag("settings_message")
                        .semantics {
                            liveRegion = if (messageIsError) LiveRegionMode.Assertive
                            else LiveRegionMode.Polite
                        },
                )
            }
            OutlinedTextField(
                baseUrl,
                { baseUrl = it },
                label = { Text("API base URL") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().testTag("api_base_url"),
            )
            OutlinedTextField(
                token,
                { token = it },
                label = { Text("Device token") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().testTag("device_token"),
                visualTransformation = if (tokenVisible) VisualTransformation.None else PasswordVisualTransformation(),
                trailingIcon = {
                    IconButton(onClick = { tokenVisible = !tokenVisible }, modifier = Modifier.testTag("device_token_visibility")) {
                        Icon(if (tokenVisible) Icons.Rounded.VisibilityOff else Icons.Rounded.Visibility, if (tokenVisible) "Hide device token" else "Show device token")
                    }
                },
            )
            Button(
                onClick = {
                    when (val result = validateSettings(baseUrl, token, allowCleartext = allowCleartext)) {
                        is SettingsValidationResult.Invalid -> {
                            message = result.message
                            messageIsError = true
                        }
                        is SettingsValidationResult.Valid -> {
                            baseUrl = result.settings.baseUrl
                            token = result.settings.token
                            LedgerSettingsStore.save(
                                context,
                                LedgerConnectionSettings(result.settings.baseUrl, result.settings.token),
                            )
                            SyncScheduler.enqueue(context)
                            message = "Settings saved. Initial sync queued."
                            messageIsError = false
                        }
                    }
                },
                modifier = Modifier.fillMaxWidth().testTag("settings_save"),
            ) {
                Text("Save and sync")
            }
            OutlinedButton(
                onClick = {
                    when (val result = validateSettings(baseUrl, token, allowCleartext = allowCleartext)) {
                        is SettingsValidationResult.Invalid -> {
                            message = result.message
                            messageIsError = true
                        }
                        is SettingsValidationResult.Valid -> {
                            baseUrl = result.settings.baseUrl
                            token = result.settings.token
                            testingConnection = true
                            scope.launch {
                                val connected = withContext(Dispatchers.IO) {
                                    runCatching { LedgerApi(result.settings.baseUrl, result.settings.token).health() }
                                        .getOrDefault(false)
                                }
                                testingConnection = false
                                message = if (connected) {
                                    "Connection successful. Save to begin syncing."
                                } else {
                                    "Connection failed. Check the URL and device token."
                                }
                                messageIsError = !connected
                            }
                        }
                    }
                },
                enabled = !testingConnection,
                modifier = Modifier.fillMaxWidth().testTag("settings_test_connection"),
            ) {
                Text(if (testingConnection) "Testing connection…" else "Test connection")
            }
        }
        LedgerCard {
            LedgerSectionHeader(
                title = "Notification capture",
                subtitle = "Allowlisted bank notifications stay local until you review them.",
            )
            OutlinedButton(
                onClick = openDiagnostics,
                modifier = Modifier.fillMaxWidth().testTag("open_diagnostics"),
            ) {
                Text("Open notification diagnostics")
            }
        }
    }
}

@Composable
private fun TransactionRow(tx: TransactionEntity, onClick: (() -> Unit)? = null) {
    val tint = when {
        tx.ledgerRole == "self_transfer_principal" -> MaterialTheme.colorScheme.primary
        tx.amountMinor < 0 -> Expense
        else -> Income
    }
    val modifier = Modifier
        .fillMaxWidth()
        .testTag("transaction_item_${tx.id}")
        .let {
            if (onClick != null) {
                it.clickable(role = Role.Button, onClick = onClick)
                    .semantics {
                        contentDescription =
                            "Open ${tx.merchant} transaction, ${money(tx.amountMinor)}, ${tx.category}"
                    }
            } else {
                it
            }
        }
    LedgerCard(modifier = modifier, contentPadding = 0.dp) {
        ListItem(
            headlineContent = { Text(tx.merchant, fontWeight = FontWeight.SemiBold) },
            supportingContent = {
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(tx.category)
                    Surface(
                        shape = MaterialTheme.shapes.small,
                        color = MaterialTheme.colorScheme.secondaryContainer,
                    ) {
                        Text(
                            tx.provenance().label,
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSecondaryContainer,
                            modifier = Modifier.padding(horizontal = 7.dp, vertical = 2.dp),
                        )
                    }
                }
            },
            trailingContent = {
                Text(
                    money(tx.amountMinor),
                    color = tint,
                    fontWeight = FontWeight.Bold,
                )
            },
            leadingContent = { Icon(Icons.Rounded.Circle, null, tint = tint) },
            colors = ListItemDefaults.colors(containerColor = Color.Transparent),
        )
    }
}

private fun TransactionEntity.toDetailSnapshot(): String = JSONObject()
    .put("id", id)
    .put("merchant", merchant)
    .put("amountMinor", amountMinor)
    .put("description", description)
    .put("currency", currency)
    .put("category", category)
    .put("account", account)
    .put("occurredAt", occurredAt)
    .put("syncState", syncState)
    .put("serverUpdatedAt", serverUpdatedAt)
    .put("kind", kind)
    .put("ledgerRole", ledgerRole)
    .put("transferBundleId", transferBundleId)
    .put("transferLeg", transferLeg)
    .put("source", source)
    .put("sourceRef", sourceRef)
    .put("evidenceCount", evidenceCount)
    .toString()

private fun detailSnapshotToTransaction(snapshot: String?): TransactionEntity? = runCatching {
    val value = JSONObject(snapshot.orEmpty())
    TransactionEntity(
        id = value.getString("id"),
        merchant = value.getString("merchant"),
        amountMinor = value.getLong("amountMinor"),
        description = value.optString("description"),
        currency = value.optString("currency", "IDR"),
        category = value.optString("category", "Uncategorized"),
        account = value.optString("account"),
        occurredAt = value.getLong("occurredAt"),
        syncState = value.optString("syncState", "pending"),
        serverUpdatedAt = value.optString("serverUpdatedAt").takeIf { it.isNotBlank() && it != "null" },
        kind = value.optString("kind", "expense"),
        ledgerRole = value.optString("ledgerRole", "ordinary"),
        transferBundleId = value.optString("transferBundleId").takeIf { it.isNotBlank() && it != "null" },
        transferLeg = value.optString("transferLeg").takeIf { it.isNotBlank() && it != "null" },
        source = value.optString("source", "unknown"),
        sourceRef = value.optString("sourceRef").takeIf { it.isNotBlank() && it != "null" },
        evidenceCount = value.optInt("evidenceCount", 0).coerceAtLeast(0),
    )
}.getOrNull()

private sealed interface TransactionObservation {
    object Loading : TransactionObservation
    data class Loaded(val value: TransactionEntity?) : TransactionObservation
}

@Composable
private fun TransactionDetail(transactionId: String, onBack: () -> Unit) {
    val context = LocalContext.current
    val db = LedgerDatabase.get(context)
    val scope = rememberCoroutineScope()
    val observationFlow = remember(db, transactionId) {
        db.transactionDao().observeById(transactionId)
    }
    val observationStateFlow = remember(observationFlow) {
        observationFlow.map<TransactionEntity?, TransactionObservation> { TransactionObservation.Loaded(it) }
    }
    val observation by observationStateFlow.collectAsState(initial = TransactionObservation.Loading)
    var baselineSnapshot by rememberSaveable(transactionId) { mutableStateOf<String?>(null) }
    var conflictSnapshot by rememberSaveable(transactionId) { mutableStateOf<String?>(null) }
    var baseline by remember(transactionId) { mutableStateOf(detailSnapshotToTransaction(baselineSnapshot)) }
    var initialized by rememberSaveable(transactionId) { mutableStateOf(false) }
    var description by rememberSaveable(transactionId) { mutableStateOf("") }
    var merchant by rememberSaveable(transactionId) { mutableStateOf("") }
    var category by rememberSaveable(transactionId) { mutableStateOf("") }
    var account by rememberSaveable(transactionId) { mutableStateOf("") }
    var occurredOn by rememberSaveable(transactionId) { mutableStateOf("") }
    var amount by rememberSaveable(transactionId) { mutableStateOf("") }
    var busy by rememberSaveable(transactionId) { mutableStateOf(false) }
    var message by rememberSaveable(transactionId) { mutableStateOf<String?>(null) }
    var showVoidDialog by rememberSaveable(transactionId) { mutableStateOf(false) }
    var showDiscardDialog by rememberSaveable(transactionId) { mutableStateOf(false) }
    var showDatePicker by rememberSaveable(transactionId) { mutableStateOf(false) }
    var remoteConflict by remember(transactionId) { mutableStateOf(detailSnapshotToTransaction(conflictSnapshot)) }
    var remotelyDeleted by rememberSaveable(transactionId) { mutableStateOf(false) }

    fun dateOf(value: TransactionEntity) = Instant.ofEpochMilli(value.occurredAt)
        .atZone(ZoneId.systemDefault()).toLocalDate().toString()
    fun setBaseline(value: TransactionEntity?) {
        baseline = value
        baselineSnapshot = value?.toDetailSnapshot()
    }
    fun setRemoteConflict(value: TransactionEntity?) {
        remoteConflict = value
        conflictSnapshot = value?.toDetailSnapshot()
    }
    fun load(value: TransactionEntity) {
        description = value.description
        merchant = value.merchant
        category = value.category
        account = value.account
        occurredOn = dateOf(value)
        amount = kotlin.math.abs(value.amountMinor).toString()
        setBaseline(value)
        setRemoteConflict(null)
        remotelyDeleted = false
        initialized = true
    }
    fun isDirty(value: TransactionEntity): Boolean =
        description != value.description || merchant != value.merchant || category != value.category ||
            account != value.account || occurredOn != dateOf(value) ||
            amount != kotlin.math.abs(value.amountMinor).toString()

    val current = baseline
    val dirty = current?.let(::isDirty) == true
    fun requestBack() {
        if (busy) return
        if (dirty) showDiscardDialog = true else onBack()
    }
    BackHandler(enabled = !busy) { requestBack() }
    LaunchedEffect(observation) {
        val loaded = observation as? TransactionObservation.Loaded
            ?: return@LaunchedEffect
        val observed = loaded.value
        val saved = baseline
        if (observed != null) remotelyDeleted = false
        when {
            observed == null && saved != null && dirty -> {
                remotelyDeleted = true
                message = "This transaction was removed on another device. Reload or go back before saving."
            }
            observed == null && saved != null -> {
                setBaseline(null)
                remotelyDeleted = true
                message = "This transaction no longer exists."
            }
            observed != null && !initialized -> load(observed)
            observed != null && saved == null -> {
                // The row reappeared after a confirmed deletion; show its current data.
                load(observed)
            }
            observed != null && saved != null && observed != saved && dirty -> {
                setRemoteConflict(observed)
                message = "This transaction changed on another device. Reload it before saving your edits."
            }
            observed != null && saved != null && observed != saved -> load(observed)
            observed != null && saved != null -> Unit
        }
    }

    val isPendingManual = current?.syncState == "pending" &&
        current.id.startsWith("android-manual-")
    val isSelfTransferPrincipal = current?.ledgerRole == "self_transfer_principal"
    val canMutate = !isSelfTransferPrincipal && (current?.syncState == "synced" || isPendingManual)
    Column(
        Modifier
            .fillMaxSize()
            .background(Ink)
            .verticalScroll(rememberScrollState())
            .imePadding()
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Row(verticalAlignment = Alignment.Top) {
            IconButton(onClick = ::requestBack, modifier = Modifier.testTag("transaction_back")) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, "Back") }
            Text(
                "Transaction details",
                style = MaterialTheme.typography.headlineSmall,
                fontWeight = FontWeight.Bold,
                modifier = Modifier
                    .weight(1f)
                    .padding(top = 8.dp)
                    .semantics { heading() },
            )
        }
        if (current == null) {
            if (observation is TransactionObservation.Loading) {
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(modifier = Modifier.size(24.dp), strokeWidth = 2.dp)
                    Text("Loading transaction…", color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
            } else {
                Text("Transaction not found", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        } else {
            LedgerCard(modifier = Modifier.fillMaxWidth()) {
                val provenance = current.provenance()
                Text("Provenance", style = MaterialTheme.typography.titleSmall)
                Text(provenance.label, fontWeight = FontWeight.SemiBold)
                Text(
                    provenance.detail,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            OutlinedTextField(description, { description = it }, label = { Text("Description") }, enabled = canMutate, modifier = Modifier.fillMaxWidth().testTag("transaction_description"))
            OutlinedTextField(merchant, { merchant = it }, label = { Text("Merchant") }, singleLine = true, enabled = canMutate, modifier = Modifier.fillMaxWidth().testTag("transaction_merchant"))
            LedgerIdrAmountField(amount, { amount = it }, modifier = Modifier.fillMaxWidth(), testTag = "transaction_amount", enabled = canMutate)
            OutlinedTextField(category, { category = it }, label = { Text("Category") }, singleLine = true, enabled = canMutate, modifier = Modifier.fillMaxWidth().testTag("transaction_category"))
            OutlinedTextField(account, { account = it }, label = { Text("Account") }, singleLine = true, enabled = canMutate, modifier = Modifier.fillMaxWidth().testTag("transaction_account"))
            LedgerDateField(occurredOn, { occurredOn = it }, { showDatePicker = true }, modifier = Modifier.fillMaxWidth(), testTag = "transaction_date", enabled = canMutate)
            Text("${current.syncState} · ${Instant.ofEpochMilli(current.occurredAt).atZone(ZoneId.systemDefault()).toLocalDate()}", color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.testTag("transaction_metadata"))
            if (isSelfTransferPrincipal) {
                LedgerCard(modifier = Modifier.fillMaxWidth()) {
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.Top) {
                        Icon(Icons.Rounded.SwapHoriz, contentDescription = null, tint = MaterialTheme.colorScheme.primary)
                        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text("Self-transfer leg", style = MaterialTheme.typography.titleSmall)
                            Text(
                                "This account movement is kept out of spending totals. Edit the transfer from the original bank event so both account legs stay balanced.",
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
            if (remoteConflict != null || remotelyDeleted) {
                OutlinedButton(
                    onClick = {
                        (observation as? TransactionObservation.Loaded)?.value?.let(::load) ?: run {
                            setBaseline(null)
                            remotelyDeleted = true
                        }
                    },
                    modifier = Modifier.fillMaxWidth().testTag("transaction_reload_remote"),
                ) { Text(if ((observation as? TransactionObservation.Loaded)?.value == null) "Acknowledge removal" else "Reload remote version") }
            }
            Button(
                onClick = {
                    val amountIdr = amount.toLongOrNull()
                    val parsedDate = runCatching { LocalDate.parse(occurredOn) }.getOrNull()
                    if (
                        description.isBlank() ||
                        merchant.isBlank() ||
                        category.isBlank() ||
                        account.isBlank() ||
                        amountIdr == null ||
                        amountIdr <= 0 ||
                        parsedDate == null
                    ) {
                        message = "Complete every field with a positive amount and valid date"
                    } else if (remoteConflict != null || remotelyDeleted) {
                        message = "Reload the latest transaction before saving."
                    } else if (!canMutate) {
                        message = "Wait for the initial sync before editing"
                    } else {
                        busy = true
                        scope.launch {
                            try {
                                if (current.syncState == "synced") {
                                val changes = JSONObject()
                                    .put("description", description.trim())
                                    .put("merchant", merchant.trim())
                                    .put("subcategory", category.trim())
                                    .put("account", account.trim())
                                    .put("occurred_on", occurredOn)
                                    .put("amount_idr", amountIdr)
                                current.serverUpdatedAt?.let { changes.put("expected_updated_at", it) }
                                val signedAmount = if (current.amountMinor < 0) -amountIdr else amountIdr
                                val updated = current.copy(
                                    merchant = merchant.trim(),
                                    amountMinor = signedAmount,
                                    description = description.trim(),
                                    category = category.trim(),
                                    account = account.trim(),
                                    occurredAt = parsedDate
                                        .atStartOfDay(ZoneId.systemDefault())
                                        .toInstant().toEpochMilli(),
                                    syncState = "pending",
                                )
                                db.withTransaction {
                                    db.transactionDao().upsert(updated)
                                    db.syncDao().enqueue(
                                        SyncOperation(
                                            kind = "transaction_update",
                                            entityId = current.id,
                                            payload = changes.toString(),
                                        )
                                    )
                                }
                                setBaseline(updated)
                                setRemoteConflict(null)
                                SyncScheduler.enqueue(context)
                                message = "Saved locally; sync queued"
                                } else {
                                val draft = ManualTransactionDraft(
                                    kind = if (current.amountMinor < 0) {
                                        ManualTransactionKind.EXPENSE
                                    } else {
                                        ManualTransactionKind.INCOME
                                    },
                                    description = description,
                                    merchant = merchant,
                                    amountIdr = amountIdr,
                                    occurredOn = occurredOn,
                                    category = category,
                                    account = account,
                                )
                                when (
                                    ManualTransactionStore(db)
                                        .updatePendingManual(current.id, draft)
                                ) {
                                    PendingManualMutationResult.APPLIED -> {
                                        setBaseline(db.transactionDao().findById(current.id))
                                        SyncScheduler.enqueue(context)
                                        message = "Updated before initial sync"
                                    }
                                    PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS -> {
                                        message = "Initial sync is in progress; try again shortly"
                                    }
                                    PendingManualMutationResult.INVALID_DRAFT -> {
                                        message = "Complete every field with a positive amount and valid date"
                                    }
                                    PendingManualMutationResult.NOT_PENDING_MANUAL -> {
                                        message = "This pending transaction cannot be edited offline"
                                    }
                                }
                                }
                            } catch (error: Exception) {
                                message = "Could not save changes. Try again."
                            } finally {
                                withContext(Dispatchers.Main.immediate) { busy = false }
                            }
                        }
                    }
                }, enabled = !busy && canMutate && remoteConflict == null && !remotelyDeleted, modifier = Modifier.fillMaxWidth().testTag("transaction_save"),
            ) { Text(if (busy) "Saving…" else "Save changes") }
            OutlinedButton(onClick = { showVoidDialog = true }, enabled = !busy && canMutate && remoteConflict == null && !remotelyDeleted, modifier = Modifier.fillMaxWidth().testTag("transaction_void")) { Text("Void transaction") }
            message?.let {
                Text(
                    it,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier
                        .testTag("transaction_message")
                        .semantics { liveRegion = LiveRegionMode.Polite },
                )
            }
        }
    }
    if (showDatePicker) {
        LedgerDatePickerDialog(
            currentValue = occurredOn,
            onDateSelected = { occurredOn = it },
            onDismiss = { showDatePicker = false },
            testTag = "transaction_date",
        )
    }
    if (showVoidDialog && current != null) AlertDialog(
        onDismissRequest = { showVoidDialog = false },
        title = { Text("Void transaction?") },
        text = { Text("This removes the transaction from your ledger.") },
        confirmButton = {
            TextButton(modifier = Modifier.testTag("transaction_void_confirm"), onClick = {
                showVoidDialog = false
                if (!busy) {
                    busy = true
                    scope.launch {
                        try {
                            if (current.syncState == "synced") {
                                db.withTransaction {
                                    db.transactionDao().delete(current.id)
                                    db.syncDao().enqueue(
                                        SyncOperation(
                                            kind = "transaction_void",
                                            entityId = current.id,
                                            payload = JSONObject().apply {
                                                current.serverUpdatedAt?.let {
                                                    put("expected_updated_at", it)
                                                }
                                            }.toString(),
                                        )
                                    )
                                }
                                withContext(Dispatchers.Main.immediate) {
                                    SyncScheduler.enqueue(context)
                                    onBack()
                                }
                            } else {
                                when (ManualTransactionStore(db).voidPendingManual(current.id)) {
                                    PendingManualMutationResult.APPLIED -> withContext(Dispatchers.Main.immediate) {
                                        onBack()
                                    }
                                    PendingManualMutationResult.INITIAL_SYNC_IN_PROGRESS -> {
                                        withContext(Dispatchers.Main.immediate) {
                                            message = "Initial sync is in progress; try again shortly"
                                        }
                                    }
                                    PendingManualMutationResult.NOT_PENDING_MANUAL -> {
                                        withContext(Dispatchers.Main.immediate) {
                                            message = "This pending transaction cannot be voided offline"
                                        }
                                    }
                                    PendingManualMutationResult.INVALID_DRAFT -> {
                                        withContext(Dispatchers.Main.immediate) {
                                            message = "This pending transaction could not be voided"
                                        }
                                    }
                                }
                            }
                        } catch (error: Exception) {
                            withContext(Dispatchers.Main.immediate) {
                                message = "Could not void this transaction. Try again."
                            }
                        } finally {
                            withContext(Dispatchers.Main.immediate) { busy = false }
                        }
                    }
                }
            }) { Text("Void") }
        },
        dismissButton = { TextButton(onClick = { showVoidDialog = false }, modifier = Modifier.testTag("transaction_void_cancel")) { Text("Cancel") } },
    )
    if (showDiscardDialog) AlertDialog(
        onDismissRequest = { showDiscardDialog = false },
        title = { Text("Discard unsaved changes?") },
        text = { Text("Your edits have not been saved to this device yet.") },
        confirmButton = {
            TextButton(
                onClick = { showDiscardDialog = false; onBack() },
                modifier = Modifier.testTag("transaction_discard_confirm"),
            ) { Text("Discard") }
        },
        dismissButton = {
            TextButton(onClick = { showDiscardDialog = false }) { Text("Keep editing") }
        },
    )
}
