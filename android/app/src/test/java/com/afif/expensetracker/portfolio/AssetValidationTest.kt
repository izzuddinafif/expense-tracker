package com.afif.expensetracker.portfolio

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

class AssetValidationTest {
    @Test
    fun unvaluedGoldIsValidAndStaysNullRatherThanZero() {
        val result = validateAssetDraft(AssetDraft("Gold", "Gold", "", "2", "gram", "2026-08-14", "", false))

        val valid = assertIs<AssetValidationResult.Valid>(result)
        assertEquals(null, valid.asset.valueIdr)
    }

    @Test
    fun invalidAssetExplainsTheBrokenField() {
        val result = validateAssetDraft(AssetDraft("", "Gold", "-1", "0", "", "today", "", false))

        assertEquals("Nama aset wajib diisi", assertIs<AssetValidationResult.Invalid>(result).message)
    }

    @Test
    fun payloadUsesTheBackendAssetFieldNamesAndKeepsUnvaluedAmountNull() {
        val validated = assertIs<AssetValidationResult.Valid>(
            validateAssetDraft(AssetDraft("Gold", "Gold", "", "2", "gram", "2026-08-14", "Vault", false)),
        ).asset
        val payload = validated.toAssetPayload()

        assertTrue(payload.has("name") && payload.has("type") && payload.has("value_idr"))
        assertTrue(payload.has("quantity") && payload.has("unit") && payload.has("last_updated"))
        assertTrue(payload.has("notes") && payload.has("is_liability"))
        assertTrue(payload.isNull("value_idr"))
    }
}
