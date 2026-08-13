package com.afif.expensetracker.budget

import com.afif.expensetracker.sync.BudgetResponse
import com.afif.expensetracker.sync.LedgerApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Thin remote repository. Budgets deliberately have no Room cache: the server is authoritative. */
class BudgetRepository(private val api: LedgerApi) {
    suspend fun list(month: String): Result<BudgetResponse> = call { api.listBudgets(month) }
    suspend fun upsert(month: String, category: String, amountIdr: Long): Result<BudgetResponse> =
        call { api.upsertBudget(month, category, amountIdr) }
    suspend fun delete(month: String, category: String): Result<BudgetResponse> =
        call { api.deleteBudget(month, category) }

    private suspend fun call(block: () -> BudgetResponse?): Result<BudgetResponse> = withContext(Dispatchers.IO) {
        runCatching { block() ?: error(api.lastError ?: "Unable to reach the ledger server") }
    }
}
