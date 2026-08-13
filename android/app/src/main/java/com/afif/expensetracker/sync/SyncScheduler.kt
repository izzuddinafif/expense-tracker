package com.afif.expensetracker.sync

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit

object SyncScheduler {
    fun enqueue(context: Context) {
        schedulePeriodic(context)
        val request = OneTimeWorkRequestBuilder<SyncWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(androidx.work.BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build()
        // A confirmation can arrive after a running worker has drained its
        // outbox. APPEND_OR_REPLACE guarantees that request runs afterwards;
        // KEEP could silently discard it during that small window.
        WorkManager.getInstance(context).enqueueUniqueWork(
            "ledger-sync",
            ExistingWorkPolicy.APPEND_OR_REPLACE,
            request,
        )
    }

    /** Keep the local ledger fresh even when no notification arrives. */
    fun schedulePeriodic(context: Context) {
        val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(androidx.work.BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            "ledger-sync-periodic",
            androidx.work.ExistingPeriodicWorkPolicy.KEEP,
            request,
        )
    }
}
