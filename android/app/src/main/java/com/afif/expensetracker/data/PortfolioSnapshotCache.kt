package com.afif.expensetracker.data

import android.content.Context
import com.afif.expensetracker.sync.LedgerAsset
import com.afif.expensetracker.sync.PortfolioAccount
import com.afif.expensetracker.sync.PortfolioFreshness
import com.afif.expensetracker.sync.PortfolioSnapshot
import com.afif.expensetracker.sync.parsePortfolioSnapshot
import org.json.JSONArray
import org.json.JSONObject

data class CachedPortfolioSnapshot(val snapshot: PortfolioSnapshot, val cachedAt: Long)

/** Non-sensitive offline projection of the latest server financial-position response. */
class PortfolioSnapshotCache(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    fun read(baseUrl: String): CachedPortfolioSnapshot? {
        if (baseUrl.isBlank() || preferences.getString(BASE_URL, null) != baseUrl.trimEnd('/')) return null
        val raw = preferences.getString(SNAPSHOT, null) ?: return null
        return runCatching {
            CachedPortfolioSnapshot(parsePortfolioSnapshot(JSONObject(raw)), preferences.getLong(CACHED_AT, 0L))
        }.getOrNull()
    }

    fun save(baseUrl: String, snapshot: PortfolioSnapshot) {
        if (baseUrl.isBlank()) return
        preferences.edit()
            .putString(BASE_URL, baseUrl.trimEnd('/'))
            .putString(SNAPSHOT, snapshot.toJson().toString())
            .putLong(CACHED_AT, System.currentTimeMillis())
            .apply()
    }

    private fun PortfolioSnapshot.toJson(): JSONObject = JSONObject()
        .put("as_of", asOf)
        .put("source", source)
        .put("freshness", freshness.name.lowercase())
        .put("accounts", JSONArray(accounts.map { it.toJson() }))
        .put("assets", JSONArray(assets.map { it.toJson() }))
        .put("total_liquid_idr", totalLiquidIdr ?: JSONObject.NULL)
        .put("total_assets_idr", totalAssetsIdr ?: JSONObject.NULL)
        .put("total_liabilities_idr", totalLiabilitiesIdr)
        .put("net_worth_idr", netWorthIdr ?: JSONObject.NULL)
        .put("warnings", JSONArray(warnings))

    private fun PortfolioAccount.toJson(): JSONObject = JSONObject()
        .put("name", name).put("type", type).put("balance_idr", balanceIdr ?: JSONObject.NULL)
        .put("initial_amount_idr", initialAmountIdr ?: JSONObject.NULL)
        .put("total_income_idr", totalIncomeIdr ?: JSONObject.NULL)
        .put("total_expenses_idr", totalExpensesIdr ?: JSONObject.NULL)
        .put("source", source).put("as_of", asOf ?: JSONObject.NULL)

    private fun LedgerAsset.toJson(): JSONObject = JSONObject()
        .put("id", id).put("name", name).put("type", type).put("value_idr", valueIdr ?: JSONObject.NULL)
        .put("quantity", quantity ?: JSONObject.NULL).put("unit", unit).put("last_updated", lastUpdated ?: JSONObject.NULL)
        .put("notes", notes).put("source", source).put("is_liability", isLiability)

    private companion object {
        const val PREFERENCES = "portfolio_snapshot"
        const val BASE_URL = "base_url"
        const val SNAPSHOT = "snapshot"
        const val CACHED_AT = "cached_at"
    }
}
