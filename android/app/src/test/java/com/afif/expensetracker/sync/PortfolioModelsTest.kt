package com.afif.expensetracker.sync

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue
import org.json.JSONObject

class PortfolioModelsTest {
    @Test
    fun portfolioParsingKeepsIntegerIdrAndUnvaluedAssetsNull() {
        val snapshot = parsePortfolioSnapshot(JSONObject("""
            {
              "as_of":"2026-08-14T08:00:00Z", "source":"sqlite", "freshness":"partial",
              "accounts":[{"name":"Mandiri","type":"bank","balance_idr":1250000,"initial_amount_idr":null,"total_income_idr":"1500000","total_expenses_idr":250000,"source":"ledger","as_of":"2026-08-14T08:00:00Z"}],
              "assets":[{"id":"gold-1","name":"Gold","type":"Gold","value_idr":null,"quantity":2,"unit":"gram","last_updated":null,"notes":"","source":"manual","is_liability":false}],
              "total_liquid_idr":1250000,"total_assets_idr":0,"total_liabilities_idr":0,"net_worth_idr":1250000,"warnings":["Gold needs valuation"]
            }
        """))

        assertEquals(PortfolioFreshness.PARTIAL, snapshot.freshness)
        assertEquals(1_250_000L, snapshot.accounts.single().balanceIdr)
        assertEquals(1_500_000L, snapshot.accounts.single().totalIncomeIdr)
        assertEquals(0L, snapshot.accounts.single().initialAmountIdr)
        assertNull(snapshot.assets.single().valueIdr)
        assertEquals("Gold needs valuation", snapshot.warnings.single())
    }

    @Test
    fun sourceFieldsArePreservedWhenTransactionsArePulled() {
        val transaction = LedgerApi("https://ledger.example", "token").parseTransaction(JSONObject("""
            {"id":"tx-1","kind":"expense","amount_idr":12000,"occurred_on":"2026-08-14",
             "description":"Lunch","merchant":"Warung","subcategory":"Food","account":"Cash",
             "source":"bank_notification","source_ref":"opaque-server-reference","evidence_count":2}
        """))

        assertEquals("bank_notification", transaction.source)
        assertEquals("opaque-server-reference", transaction.sourceRef)
        assertEquals(2, transaction.evidenceCount)
        assertTrue(transaction.amountMinor < 0)
    }
}
