package com.afif.expensetracker.notification

import com.afif.expensetracker.data.NotificationDao
import com.afif.expensetracker.data.NotificationRecord
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotEquals

class NotificationIngestorTest {
    @Test
    fun reusedKeyWithChangedContentCreatesAnotherCapture() = runBlocking {
        val dao = FakeNotificationDao()
        val ingestor = NotificationIngestor(dao) { 1_721_065_200_000L }
        val packageName = BankNotificationSources.BSI_BYOND_PACKAGE
        val initialBody = "Pembayaran Rp25.000 di TOKO MAJU pada 12/07/2026"
        val updatedBody = "Pembayaran Rp26.000 di TOKO BARU pada 12/07/2026"

        val first = ingestor.ingest(packageName, "BYOND by BSI", initialBody, "notification-key-1")
        val repeated = ingestor.ingest(packageName, "BYOND by BSI", initialBody, "notification-key-1")
        val updated = ingestor.ingest(packageName, "BYOND by BSI", updatedBody, "notification-key-1")
        val distinct = ingestor.ingest(packageName, "BYOND by BSI", updatedBody, "notification-key-2")

        assertEquals(first, repeated)
        assertNotEquals(first, updated)
        assertNotEquals(updated, distinct)
        assertEquals(3, dao.records.size)
        assertEquals(initialBody, dao.records.single { it.id == first }.body)
        assertEquals(25_000L, dao.records.single { it.id == first }.amountIdr)
        assertEquals(updated, dao.records.single { it.id == distinct }.suspectedDuplicateOf)
    }

    @Test
    fun reusedKeyCannotRewriteCompletedCapture() = runBlocking {
        val dao = FakeNotificationDao()
        val ingestor = NotificationIngestor(dao) { 1_721_065_200_000L }
        val packageName = BankNotificationSources.BSI_BYOND_PACKAGE
        val originalBody = "Pembayaran Rp25.000 di TOKO MAJU pada 12/07/2026"
        val first = requireNotNull(
            ingestor.ingest(packageName, "BYOND by BSI", originalBody, "notification-key-1"),
        )
        dao.updateStatus(first, "confirmed")

        val repeated = ingestor.ingest(
            packageName,
            "BYOND by BSI",
            "Pembayaran Rp99.000 di TOKO LAIN pada 12/07/2026",
            "notification-key-1",
        )

        assertNotEquals(first, repeated)
        assertEquals(2, dao.records.size)
        assertEquals("confirmed", dao.records.single { it.id == first }.status)
        assertEquals(originalBody, dao.records.single { it.id == first }.body)
        assertEquals(25_000L, dao.records.single { it.id == first }.amountIdr)
    }

    private class FakeNotificationDao : NotificationDao {
        val records = mutableListOf<NotificationRecord>()
        private var nextId = 1L

        override suspend fun enqueue(record: NotificationRecord): Long {
            if (records.any {
                    it.sourceRef == record.sourceRef ||
                        (record.platformIdentityRef != null && it.platformIdentityRef == record.platformIdentityRef)
                }
            ) return -1L
            val id = nextId++
            records += record.copy(id = id)
            return id
        }

        override fun observeRecent(limit: Int): Flow<List<NotificationRecord>> =
            flowOf(records.sortedByDescending(NotificationRecord::receivedAt).take(limit))

        override fun observeByStatus(status: String, limit: Int): Flow<List<NotificationRecord>> =
            flowOf(records.filter { it.status == status }.take(limit))

        override suspend fun findById(id: Long): NotificationRecord? = records.find { it.id == id }

        override suspend fun findBySourceRef(sourceRef: String): NotificationRecord? =
            records.find { it.sourceRef == sourceRef }

        override suspend fun findByPlatformIdentityRef(platformIdentityRef: String): NotificationRecord? =
            records.find { it.platformIdentityRef == platformIdentityRef }

        override suspend fun findRecentForPackage(
            packageName: String,
            receivedAfter: Long,
            limit: Int,
        ): List<NotificationRecord> = records
            .filter { it.packageName == packageName && it.receivedAt >= receivedAfter }
            .sortedByDescending(NotificationRecord::receivedAt)
            .take(limit)

        override suspend fun refreshPendingCapture(
            id: Long,
            title: String,
            body: String,
            amountIdr: Long?,
            merchant: String?,
            bank: String,
            direction: String,
            occurredOn: String?,
            reviewRequired: Boolean,
        ): Int {
            val index = records.indexOfFirst { it.id == id && it.status == "pending" }
            if (index == -1) return 0
            records[index] = records[index].copy(
                title = title,
                body = body,
                amountIdr = amountIdr,
                merchant = merchant,
                bank = bank,
                direction = direction,
                occurredOn = occurredOn,
                reviewRequired = reviewRequired,
            )
            return 1
        }

        override suspend fun updateStatus(id: Long, status: String) {
            val index = records.indexOfFirst { it.id == id }
            if (index != -1) records[index] = records[index].copy(status = status)
        }
    }
}
