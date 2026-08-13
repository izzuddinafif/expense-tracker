package com.afif.expensetracker.ui.components

import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CalendarMonth
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import com.afif.expensetracker.manual.formatIdrPreview
import com.afif.expensetracker.manual.toUtcMidnightEpochMillis
import com.afif.expensetracker.manual.utcMidnightEpochMillisToLocalDate
import java.time.LocalDate

/** Consistent numeric input and non-sensitive IDR preview for ledger forms. */
@Composable
fun LedgerIdrAmountField(
    value: String,
    onValueChange: (String) -> Unit,
    modifier: Modifier = Modifier,
    testTag: String,
    enabled: Boolean = true,
    imeAction: ImeAction = ImeAction.Next,
) {
    OutlinedTextField(
        value = value,
        onValueChange = { onValueChange(it.filter(Char::isDigit)) },
        label = { Text("Amount (IDR)") },
        modifier = modifier.testTag(testTag),
        singleLine = true,
        enabled = enabled,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number, imeAction = imeAction),
        supportingText = formatIdrPreview(value)?.let { preview ->
            { Text(preview, modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) }
        },
    )
}

/** Text date input with a shared calendar affordance; ISO input remains keyboard-accessible. */
@Composable
fun LedgerDateField(
    value: String,
    onValueChange: (String) -> Unit,
    onOpenPicker: () -> Unit,
    modifier: Modifier = Modifier,
    testTag: String,
    enabled: Boolean = true,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text("Date (YYYY-MM-DD)") },
        modifier = modifier.testTag(testTag),
        singleLine = true,
        enabled = enabled,
        trailingIcon = {
            IconButton(onClick = onOpenPicker, enabled = enabled, modifier = Modifier.testTag("${testTag}_picker_open")) {
                Icon(Icons.Rounded.CalendarMonth, contentDescription = "Choose date")
            }
        },
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LedgerDatePickerDialog(
    currentValue: String,
    onDateSelected: (String) -> Unit,
    onDismiss: () -> Unit,
    testTag: String,
) {
    val selected = runCatching { LocalDate.parse(currentValue) }.getOrDefault(LocalDate.now())
    val state = androidx.compose.material3.rememberDatePickerState(
        initialSelectedDateMillis = selected.toUtcMidnightEpochMillis(),
    )
    DatePickerDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = {
                    state.selectedDateMillis?.let {
                        onDateSelected(utcMidnightEpochMillisToLocalDate(it).toString())
                    }
                    onDismiss()
                },
                modifier = Modifier.testTag("${testTag}_picker_confirm"),
            ) { Text("Use date") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    ) {
        DatePicker(state = state, modifier = Modifier.testTag("${testTag}_picker"))
    }
}
