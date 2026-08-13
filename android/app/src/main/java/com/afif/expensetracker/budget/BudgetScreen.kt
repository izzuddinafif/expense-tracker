package com.afif.expensetracker.budget

import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.KeyboardArrowLeft
import androidx.compose.material.icons.automirrored.rounded.KeyboardArrowRight
import androidx.compose.material.icons.rounded.Add
import androidx.compose.material.icons.rounded.DeleteOutline
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.afif.expensetracker.sync.LedgerApi
import com.afif.expensetracker.sync.MonthlyBudget
import com.afif.expensetracker.data.LedgerSettingsStore
import com.afif.expensetracker.ui.components.LedgerCard
import com.afif.expensetracker.ui.components.LedgerIdrAmountField
import com.afif.expensetracker.ui.theme.*
import java.text.NumberFormat
import java.time.YearMonth
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

private fun budgetMoney(value: Long) = NumberFormat.getCurrencyInstance(Locale("id", "ID")).apply { maximumFractionDigits = 0 }.format(value)

/**
 * Server responses are only applied when they still belong to the visible month.
 * Cancelling is an efficiency measure; the request version is the correctness guard.
 */
@Composable
fun BudgetScreen(onOpenSettings: () -> Unit = {}) {
    val context = LocalContext.current
    val settings = remember { LedgerSettingsStore.read(context) }
    val baseUrl = settings.baseUrl
    val token = settings.token
    if (baseUrl.isBlank() || token.isBlank()) {
        BudgetMessage(
            "Connect the ledger first",
            "Add your API base URL and device token in Settings.",
            actionLabel = "Open Settings",
            action = onOpenSettings,
        )
        return
    }
    val repository = remember(baseUrl, token) { BudgetRepository(LedgerApi(baseUrl, token)) }
    var monthValue by rememberSaveable { mutableStateOf(YearMonth.now().toString()) }
    val month = remember(monthValue) { YearMonth.parse(monthValue) }
    var report by remember { mutableStateOf<List<MonthlyBudget>?>(null) }
    var error by rememberSaveable { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    var showEditor by rememberSaveable { mutableStateOf(false) }
    var editingCategory by rememberSaveable { mutableStateOf<String?>(null) }
    var deleteCategory by rememberSaveable { mutableStateOf<String?>(null) }
    var requestVersion by rememberSaveable { mutableIntStateOf(0) }
    var activeRequest by remember { mutableStateOf<Job?>(null) }
    val scope = rememberCoroutineScope()

    fun startRequest(targetMonth: YearMonth, request: suspend () -> Result<List<MonthlyBudget>>, closeEditor: Boolean = false) {
        activeRequest?.cancel()
        val version = ++requestVersion
        loading = true
        error = null
        activeRequest = scope.launch {
            request().onSuccess { budgets ->
                if (version == requestVersion && month == targetMonth) {
                    report = budgets
                    if (closeEditor) showEditor = false
                }
            }.onFailure { failure ->
                if (version == requestVersion && month == targetMonth) {
                    error = failure.message ?: "Request failed"
                }
            }
            if (version == requestVersion && month == targetMonth) loading = false
        }
    }
    fun refresh(targetMonth: YearMonth = month) = startRequest(targetMonth, {
        repository.list(targetMonth.toString()).map { it.budgets }
    })
    fun changeMonth(value: YearMonth) {
        activeRequest?.cancel()
        ++requestVersion
        monthValue = value.toString()
        report = null
        error = null
        loading = true
    }

    LaunchedEffect(month, repository) { refresh(month) }
    val rows = report.orEmpty()
    val editing = editingCategory?.let { category -> rows.firstOrNull { it.category == category } }
    val deleteTarget = deleteCategory?.let { category -> rows.firstOrNull { it.category == category } }
    LazyColumn(Modifier.fillMaxSize().background(Ink).padding(20.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
        item {
            Text("Budgets", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
            Text("Server-authoritative monthly guardrails", color = MaterialTheme.colorScheme.onSurfaceVariant)
            Spacer(Modifier.height(12.dp))
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.SpaceBetween) {
                IconButton(onClick = { changeMonth(month.minusMonths(1)) }, modifier = Modifier.testTag("budget_previous_month")) { Icon(Icons.AutoMirrored.Rounded.KeyboardArrowLeft, "Previous month") }
                Text(
                    month.format(DateTimeFormatter.ofPattern("MMMM yyyy", Locale.ENGLISH))
                        .replaceFirstChar { it.uppercase() },
                    style = MaterialTheme.typography.titleLarge,
                    fontWeight = FontWeight.SemiBold,
                    modifier = Modifier.testTag("budget_month"),
                )
                IconButton(onClick = { changeMonth(month.plusMonths(1)) }, modifier = Modifier.testTag("budget_next_month")) { Icon(Icons.AutoMirrored.Rounded.KeyboardArrowRight, "Next month") }
            }
        }
        if (loading) item { LinearProgressIndicator(Modifier.fillMaxWidth().testTag("budget_loading"), color = MaterialTheme.colorScheme.primary) }
        error?.let { message -> item { BudgetError(message) { refresh() } } }
        if (!loading && error == null && rows.isEmpty()) item { BudgetMessage("No budgets yet", "Add a category to start watching your monthly spend.") }
        items(rows, key = { it.category }) { budget -> BudgetCard(budget, { editingCategory = budget.category; showEditor = true }, { deleteCategory = budget.category }) }
        item { Button(onClick = { editingCategory = null; showEditor = true }, modifier = Modifier.fillMaxWidth().testTag("budget_add")) { Icon(Icons.Rounded.Add, null); Spacer(Modifier.width(8.dp)); Text("Add budget") } }
    }
    if (showEditor) BudgetEditor(editing, { showEditor = false }, { category, amount ->
        val targetMonth = month
        startRequest(targetMonth, {
            repository.upsert(targetMonth.toString(), category, amount).map { it.budgets }
        }, closeEditor = true)
    })
    deleteTarget?.let { target -> AlertDialog(onDismissRequest = { deleteCategory = null }, title = { Text("Delete ${target.category} budget?") }, text = { Text("This only removes the budget target; transactions stay untouched.") }, confirmButton = { TextButton(modifier = Modifier.testTag("budget_delete_confirm"), onClick = { deleteCategory = null; val targetMonth = month; startRequest(targetMonth, { repository.delete(targetMonth.toString(), target.category).map { it.budgets } }) }) { Text("Delete") } }, dismissButton = { TextButton(onClick = { deleteCategory = null }) { Text("Cancel") } }) }
}

@Composable private fun BudgetCard(budget: MonthlyBudget, edit: () -> Unit, delete: () -> Unit) {
    val bar = when (budget.status) { "over" -> Expense; "warning" -> Warning; else -> Income }
    LedgerCard(modifier = Modifier.testTag("budget_${budget.category}"), contentPadding = 16.dp) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) { Text(budget.category, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f)); TextButton(onClick = edit, modifier = Modifier.testTag("budget_edit_${budget.category}")) { Text("Edit") }; IconButton(onClick = delete, modifier = Modifier.testTag("budget_delete_${budget.category}")) { Icon(Icons.Rounded.DeleteOutline, "Delete") } }
        Text("${budgetMoney(budget.spentIdr)} of ${budgetMoney(budget.budgetIdr)}", color = MaterialTheme.colorScheme.onSurfaceVariant)
        LinearProgressIndicator(progress = { (budget.percentage / 100.0).coerceIn(0.0, 1.0).toFloat() }, modifier = Modifier.fillMaxWidth(), color = bar, trackColor = Surface)
        Text(if (budget.remainingIdr >= 0) "${budgetMoney(budget.remainingIdr)} remaining" else "${budgetMoney(-budget.remainingIdr)} over budget", color = bar, fontWeight = FontWeight.SemiBold)
    }
}

@Composable private fun BudgetEditor(existing: MonthlyBudget?, close: () -> Unit, save: (String, Long) -> Unit) {
    var category by rememberSaveable(existing?.category) { mutableStateOf(existing?.category.orEmpty()) }
    var amount by rememberSaveable(existing?.category) { mutableStateOf(existing?.budgetIdr?.toString().orEmpty()) }
    var invalid by rememberSaveable(existing?.category) { mutableStateOf(false) }
    AlertDialog(
        onDismissRequest = close,
        title = { Text(if (existing == null) "Add budget" else "Edit budget") },
        text = {
            Column(
                modifier = Modifier
                    .verticalScroll(rememberScrollState())
                    .imePadding(),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                OutlinedTextField(
                    category,
                    { category = it },
                    label = { Text("Category") },
                    enabled = existing == null,
                    singleLine = true,
                    modifier = Modifier.testTag("budget_category_input"),
                )
                LedgerIdrAmountField(
                    amount,
                    { amount = it; invalid = false },
                    modifier = Modifier.fillMaxWidth(),
                    testTag = "budget_amount_input",
                )
                if (invalid) {
                    Text(
                        "Enter a category and positive amount",
                        color = Expense,
                        modifier = Modifier.semantics { liveRegion = LiveRegionMode.Assertive },
                    )
                }
            }
        },
        confirmButton = {
            Button(
                modifier = Modifier.testTag("budget_save"),
                onClick = {
                    val value = amount.toLongOrNull()
                    if (category.isBlank() || value == null || value <= 0) invalid = true
                    else save(category.trim(), value)
                },
            ) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = close) { Text("Cancel") } },
    )
}

@Composable private fun BudgetError(message: String, retry: () -> Unit) { Column(verticalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.testTag("budget_error").semantics { liveRegion = LiveRegionMode.Assertive }) { Text(message, color = Expense); OutlinedButton(onClick = retry) { Text("Retry") } } }
@Composable
private fun BudgetMessage(
    title: String,
    body: String,
    actionLabel: String? = null,
    action: (() -> Unit)? = null,
) {
    LedgerCard(contentPadding = 18.dp) {
        Text(title, fontWeight = FontWeight.Bold)
        Text(body, color = MaterialTheme.colorScheme.onSurfaceVariant)
        if (actionLabel != null && action != null) {
            OutlinedButton(onClick = action, modifier = Modifier.testTag("budget_message_action")) {
                Text(actionLabel)
            }
        }
    }
}
