package com.afif.expensetracker.portfolio

import java.time.LocalDate

data class AssetDraft(
    val name: String,
    val type: String,
    val valueIdr: String,
    val quantity: String,
    val unit: String,
    val lastUpdated: String,
    val notes: String,
    val isLiability: Boolean,
)

data class ValidatedAssetDraft(
    val name: String,
    val type: String,
    val valueIdr: Long?,
    val quantity: Double?,
    val unit: String,
    val lastUpdated: String,
    val notes: String,
    val isLiability: Boolean,
)

sealed interface AssetValidationResult {
    data class Valid(val asset: ValidatedAssetDraft) : AssetValidationResult
    data class Invalid(val message: String) : AssetValidationResult
}

fun validateAssetDraft(draft: AssetDraft): AssetValidationResult {
    val name = draft.name.trim()
    val type = draft.type.trim()
    val unit = draft.unit.trim()
    val updated = draft.lastUpdated.trim()
    val value = draft.valueIdr.trim().takeIf(String::isNotBlank)?.toLongOrNull()
    val quantity = draft.quantity.trim().toDoubleOrNull()
    return when {
        name.isBlank() -> AssetValidationResult.Invalid("Nama aset wajib diisi")
        type.isBlank() -> AssetValidationResult.Invalid("Jenis aset wajib diisi")
        draft.valueIdr.isNotBlank() && (value == null || value < 0) -> AssetValidationResult.Invalid("Nilai harus berupa IDR nol atau lebih")
        draft.quantity.isNotBlank() && (quantity == null || quantity <= 0) -> AssetValidationResult.Invalid("Jumlah harus lebih dari nol")
        unit.isBlank() -> AssetValidationResult.Invalid("Satuan wajib diisi")
        updated.isBlank() || runCatching { LocalDate.parse(updated) }.isFailure -> AssetValidationResult.Invalid("Tanggal pembaruan harus YYYY-MM-DD")
        else -> AssetValidationResult.Valid(ValidatedAssetDraft(
            name, type, value, quantity, unit, updated, draft.notes.trim(), draft.isLiability,
        ))
    }
}
