package com.afif.expensetracker.notification

import android.app.Notification
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import com.afif.expensetracker.data.LedgerDatabase
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.cancel

/** Captures only notification metadata. Content is queued locally for review; no secrets leave the device. */
class LedgerNotificationListener : NotificationListenerService() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        val extras = sbn.notification.extras
        val title = (
            extras.getCharSequence(Notification.EXTRA_TITLE_BIG)
                ?: extras.getCharSequence(Notification.EXTRA_TITLE)
            )?.toString()?.take(160) ?: sbn.packageName
        val body = (
            extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
                ?: extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
                ?: extras.getCharSequenceArray(Notification.EXTRA_TEXT_LINES)
                    ?.joinToString("\n")
            ).orEmpty().take(500)
        scope.launch {
            NotificationIngestor(LedgerDatabase.get(applicationContext).notificationDao())
                .ingest(sbn.packageName, title, body, sourceIdentity = sbn.key)
        }
    }
    override fun onDestroy() { scope.cancel(); super.onDestroy() }
}
