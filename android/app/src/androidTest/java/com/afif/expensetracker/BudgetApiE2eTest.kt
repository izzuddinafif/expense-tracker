package com.afif.expensetracker

import androidx.test.ext.junit.runners.AndroidJUnit4
import com.afif.expensetracker.sync.LedgerApi
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BudgetApiE2eTest {
    private lateinit var server: MockWebServer
    private lateinit var api: LedgerApi

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        api = LedgerApi(server.url("/").toString(), "budget-token")
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    @Test
    fun budgetCrudUsesCanonicalContractAndEncodedQueries() {
        server.enqueue(
            jsonResponse(
                """
                {
                  "month":"2026-07",
                  "budgets":[{
                    "month":"2026-07",
                    "category":"Warung/Makan Siap Saji",
                    "amount_idr":100000,
                    "spent_idr":85000,
                    "remaining_idr":15000,
                    "percentage":85,
                    "status":"warning"
                  }]
                }
                """.trimIndent()
            )
        )

        val listed = requireNotNull(api.listBudgets("2026-07"))
        assertEquals(100_000L, listed.budgets.single().budgetIdr)
        assertEquals(85_000L, listed.budgets.single().spentIdr)
        server.takeRequest().also {
            assertEquals("Bearer budget-token", it.getHeader("Authorization"))
            assertEquals("/api/v1/budgets?month=2026-07", it.path)
        }

        server.enqueue(jsonResponse("""{"month":"2026-07","budgets":[]}"""))
        requireNotNull(api.upsertBudget("2026-07", "Dining", 250_000))
        server.takeRequest().also {
            assertEquals("PUT", it.method)
            val body = JSONObject(it.body.readUtf8())
            assertEquals("2026-07", body.getString("month"))
            assertEquals("Dining", body.getString("category"))
            assertEquals(250_000L, body.getLong("amount_idr"))
        }

        server.enqueue(
            jsonResponse(
                """{"month":"2026-07","category":"Warung/Makan Siap Saji","deleted":true,"budgets":[]}"""
            )
        )
        requireNotNull(api.deleteBudget("2026-07", "Warung/Makan Siap Saji"))
        server.takeRequest().also {
            assertEquals("DELETE", it.method)
            assertEquals("2026-07", it.requestUrl?.queryParameter("month"))
            assertEquals(
                "Warung/Makan Siap Saji",
                it.requestUrl?.queryParameter("category"),
            )
        }
    }

    private fun jsonResponse(body: String) = MockResponse()
        .setResponseCode(200)
        .setHeader("Content-Type", "application/json")
        .setBody(body)
}
