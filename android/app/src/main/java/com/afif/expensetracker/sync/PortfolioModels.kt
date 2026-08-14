package com.afif.expensetracker.sync

import org.json.JSONArray
import org.json.JSONObject

/** A server-authoritative financial-position snapshot. Amounts are whole IDR, never floats. */
data class PortfolioSnapshot(
    val asOf: String?,
    val source: String,
    val freshness: PortfolioFreshness,
    val accounts: List<PortfolioAccount>,
    val assets: List<LedgerAsset>,
    val totalLiquidIdr: Long,
    val totalAssetsIdr: Long,
    val totalLiabilitiesIdr: Long,
    val netWorthIdr: Long,
    val warnings: List<String>,
)

enum class PortfolioFreshness { LIVE, CACHED, PARTIAL;
    companion object {
        fun fromApi(value: String?) = when (value?.lowercase()) {
            "cached" -> CACHED
            "partial" -> PARTIAL
            else -> LIVE
        }
    }
}

data class PortfolioAccount(
    val name: String,
    val type: String,
    val balanceIdr: Long,
    val initialAmountIdr: Long,
    val totalIncomeIdr: Long,
    val totalExpensesIdr: Long,
    val source: String,
    val asOf: String?,
)

data class LedgerAsset(
    val id: String,
    val name: String,
    val type: String,
    val valueIdr: Long?,
    val quantity: Double,
    val unit: String,
    val lastUpdated: String?,
    val notes: String,
    val source: String,
    val isLiability: Boolean,
)

internal fun parsePortfolioSnapshot(root: JSONObject): PortfolioSnapshot = PortfolioSnapshot(
    asOf = root.optionalString("as_of"),
    source = root.optString("source", "ledger"),
    freshness = PortfolioFreshness.fromApi(root.optionalString("freshness")),
    accounts = root.optJSONArray("accounts").toPortfolioAccounts(),
    assets = root.optJSONArray("assets").toAssets(),
    totalLiquidIdr = root.idrLong("total_liquid_idr"),
    totalAssetsIdr = root.idrLong("total_assets_idr"),
    totalLiabilitiesIdr = root.idrLong("total_liabilities_idr"),
    netWorthIdr = root.idrLong("net_worth_idr"),
    warnings = root.optJSONArray("warnings").toStrings(),
)

internal fun parseAssetsResponse(root: JSONObject): List<LedgerAsset> = root.optJSONArray("assets").toAssets()

internal fun parseLedgerAsset(value: JSONObject): LedgerAsset = LedgerAsset(
    id = value.optString("id"),
    name = value.optString("name", "Untitled asset"),
    type = value.optString("type", "Other"),
    valueIdr = value.idrLongOrNull("value_idr"),
    quantity = value.optDouble("quantity", 0.0),
    unit = value.optString("unit"),
    lastUpdated = value.optionalString("last_updated"),
    notes = value.optString("notes"),
    source = value.optString("source", "manual"),
    isLiability = value.optBoolean("is_liability", false),
)

private fun JSONArray?.toPortfolioAccounts(): List<PortfolioAccount> = buildList {
    this@toPortfolioAccounts ?: return@buildList
    for (index in 0 until length()) {
        val value = optJSONObject(index) ?: continue
        add(PortfolioAccount(
            name = value.optString("name", "Account"),
            type = value.optString("type", "cash"),
            balanceIdr = value.idrLong("balance_idr"),
            initialAmountIdr = value.idrLong("initial_amount_idr"),
            totalIncomeIdr = value.idrLong("total_income_idr"),
            totalExpensesIdr = value.idrLong("total_expenses_idr"),
            source = value.optString("source", "ledger"),
            asOf = value.optionalString("as_of"),
        ))
    }
}

private fun JSONArray?.toAssets(): List<LedgerAsset> = buildList {
    this@toAssets ?: return@buildList
    for (index in 0 until length()) optJSONObject(index)?.let { add(parseLedgerAsset(it)) }
}

private fun JSONArray?.toStrings(): List<String> = buildList {
    this@toStrings ?: return@buildList
    for (index in 0 until length()) optString(index).takeIf(String::isNotBlank)?.let(::add)
}

internal fun JSONObject.idrLong(key: String): Long = idrLongOrNull(key) ?: 0L

internal fun JSONObject.idrLongOrNull(key: String): Long? {
    if (!has(key) || isNull(key)) return null
    return when (val raw = opt(key)) {
        is Number -> raw.toLong()
        is String -> raw.trim().toLongOrNull()
        else -> null
    }
}

internal fun JSONObject.optionalString(key: String): String? =
    optString(key).takeIf { it.isNotBlank() && it != "null" }
