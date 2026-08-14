@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package com.afif.expensetracker.manual

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CalendarMonth
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import java.time.LocalDate

/**
 * The single entry point for adding a transaction without relying on a bank
 * notification.  Keeping the form in a dialog makes it available from any
 * surface while the scroll container keeps it usable on compact screens.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ManualTransactionDialog(
    saving: Boolean,
    externalError: String?,
    categorySuggestions: List<String> = emptyList(),
    accountSuggestions: List<String> = emptyList(),
    onDismiss: () -> Unit,
    onConfirm: (ManualTransactionDraft) -> Unit,
) {
    var kind by remember { mutableStateOf(ManualTransactionKind.EXPENSE) }
    var description by remember { mutableStateOf("") }
    var merchant by remember { mutableStateOf("") }
    var amount by remember { mutableStateOf("") }
    var date by remember { mutableStateOf(LocalDate.now().toString()) }
    var category by remember { mutableStateOf("") }
    var account by remember { mutableStateOf("") }
    var validationError by remember { mutableStateOf<String?>(null) }
    var showDatePicker by remember { mutableStateOf(false) }

    val error = externalError?.takeIf { it.isNotBlank() } ?: validationError
    AlertDialog(
        onDismissRequest = { if (!saving) onDismiss() },
        title = { Text("Add transaction") },
        text = {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(max = 480.dp)
                    .verticalScroll(rememberScrollState())
                    .imePadding(),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                FlowRow(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    FilterChip(
                        selected = kind == ManualTransactionKind.EXPENSE,
                        onClick = { if (!saving) kind = ManualTransactionKind.EXPENSE },
                        label = { Text("Expense") },
                        enabled = !saving,
                        modifier = Modifier.testTag("manual_kind_expense"),
                    )
                    FilterChip(
                        selected = kind == ManualTransactionKind.INCOME,
                        onClick = { if (!saving) kind = ManualTransactionKind.INCOME },
                        label = { Text("Income") },
                        enabled = !saving,
                        modifier = Modifier.testTag("manual_kind_income"),
                    )
                }
                OutlinedTextField(
                    value = description,
                    onValueChange = { description = it; validationError = null },
                    label = { Text("Description") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_description"),
                    singleLine = true,
                    enabled = !saving,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                )
                OutlinedTextField(
                    value = merchant,
                    onValueChange = { merchant = it; validationError = null },
                    label = { Text("Merchant") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_merchant"),
                    singleLine = true,
                    enabled = !saving,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                )
                OutlinedTextField(
                    value = amount,
                    onValueChange = {
                        amount = it.filter { character -> character in '0'..'9' }
                        validationError = null
                    },
                    label = { Text("Amount (IDR)") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_amount"),
                    singleLine = true,
                    enabled = !saving,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = ImeAction.Next),
                    supportingText = formatIdrPreview(amount)?.let { preview ->
                        {
                            Text(
                                text = preview,
                                modifier = Modifier
                                    .testTag("manual_amount_preview")
                                    .semantics { liveRegion = LiveRegionMode.Polite },
                            )
                        }
                    },
                )
                OutlinedTextField(
                    value = date,
                    onValueChange = {},
                    label = { Text("Date") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_date"),
                    singleLine = true,
                    enabled = !saving,
                    readOnly = true,
                    trailingIcon = {
                        IconButton(
                            onClick = { showDatePicker = true },
                            enabled = !saving,
                            modifier = Modifier.testTag("manual_date_picker_open"),
                        ) {
                            Icon(
                                imageVector = Icons.Rounded.CalendarMonth,
                                contentDescription = "Choose date",
                            )
                        }
                    },
                )
                OutlinedTextField(
                    value = category,
                    onValueChange = { category = it; validationError = null },
                    label = { Text("Category") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_category"),
                    singleLine = true,
                    enabled = !saving,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
                )
                ManualSuggestionRow(
                    label = "Recent categories",
                    suggestions = categorySuggestions,
                    tagPrefix = "manual_category_suggestion",
                    enabled = !saving,
                    onSelect = {
                        category = it
                        validationError = null
                    },
                )
                OutlinedTextField(
                    value = account,
                    onValueChange = { account = it; validationError = null },
                    label = { Text("Account") },
                    modifier = Modifier.fillMaxWidth().testTag("manual_account"),
                    singleLine = true,
                    enabled = !saving,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Done),
                )
                ManualSuggestionRow(
                    label = "Accounts",
                    suggestions = accountSuggestions,
                    tagPrefix = "manual_account_suggestion",
                    enabled = !saving,
                    onSelect = {
                        account = it
                        validationError = null
                    },
                )
                error?.let {
                    Text(
                        text = it,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier
                            .testTag("manual_error")
                            .semantics { liveRegion = LiveRegionMode.Assertive },
                    )
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    val parsedAmount = amount.toLongOrNull()
                    val parsedDate = runCatching { LocalDate.parse(date.trim()) }.getOrNull()
                    val issue = when {
                        description.isBlank() -> "Add a description."
                        merchant.isBlank() -> "Add a merchant."
                        parsedAmount == null || parsedAmount <= 0L -> "Enter a positive amount."
                        parsedDate == null -> "Use a valid date such as 2026-07-29."
                        category.isBlank() -> "Add a category."
                        account.isBlank() -> "Add an account."
                        else -> null
                    }
                    validationError = issue
                    if (!saving && issue == null && parsedDate != null && parsedAmount != null) {
                        onConfirm(
                            ManualTransactionDraft(
                                kind = kind,
                                description = description.trim(),
                                merchant = merchant.trim(),
                                amountIdr = parsedAmount,
                                occurredOn = parsedDate.toString(),
                                category = category.trim(),
                                account = account.trim(),
                            ),
                        )
                    }
                },
                enabled = !saving,
                modifier = Modifier.testTag("manual_save"),
            ) { Text(if (saving) "Saving…" else "Save") }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !saving) { Text("Cancel") }
        },
    )

    if (showDatePicker) {
        val selectedDate = runCatching { LocalDate.parse(date) }.getOrDefault(LocalDate.now())
        val pickerState = rememberDatePickerState(
            initialSelectedDateMillis = selectedDate.toUtcMidnightEpochMillis(),
        )
        DatePickerDialog(
            onDismissRequest = { if (!saving) showDatePicker = false },
            confirmButton = {
                TextButton(
                    onClick = {
                        pickerState.selectedDateMillis?.let {
                            date = utcMidnightEpochMillisToLocalDate(it).toString()
                            validationError = null
                        }
                        showDatePicker = false
                    },
                    enabled = !saving,
                    modifier = Modifier.testTag("manual_date_picker_confirm"),
                ) {
                    Text("Use date")
                }
            },
            dismissButton = {
                TextButton(
                    onClick = { showDatePicker = false },
                    enabled = !saving,
                    modifier = Modifier.testTag("manual_date_picker_cancel"),
                ) {
                    Text("Cancel")
                }
            },
        ) {
            DatePicker(
                state = pickerState,
                modifier = Modifier.testTag("manual_date_picker"),
            )
        }
    }
}

@Composable
private fun ManualSuggestionRow(
    label: String,
    suggestions: List<String>,
    tagPrefix: String,
    enabled: Boolean,
    onSelect: (String) -> Unit,
) {
    if (suggestions.isEmpty()) return
    Text(
        text = label,
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        suggestions.forEachIndexed { index, suggestion ->
            SuggestionChip(
                onClick = { onSelect(suggestion) },
                label = { Text(suggestion) },
                enabled = enabled,
                modifier = Modifier.testTag("${tagPrefix}_$index"),
            )
        }
    }
}
