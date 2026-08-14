package com.afif.expensetracker.data

import android.content.Context
import androidx.sqlite.db.SupportSQLiteDatabase
import androidx.sqlite.db.SupportSQLiteOpenHelper
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class LedgerDatabaseMigrationTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val databaseName = "ledger-migration-9-10-test.db"

    @Before
    fun setUp() {
        context.deleteDatabase(databaseName)
    }

    @After
    fun tearDown() {
        context.deleteDatabase(databaseName)
    }

    @Test
    fun migration9To10PreservesTransactionsAndAddsServerUpdatedAt() {
        openDatabase(version = 9) { database ->
            database.execSQL(
                """
                CREATE TABLE transactions (
                    id TEXT NOT NULL PRIMARY KEY,
                    merchant TEXT NOT NULL,
                    amountMinor INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    category TEXT NOT NULL,
                    account TEXT NOT NULL,
                    occurredAt INTEGER NOT NULL,
                    syncState TEXT NOT NULL
                )
                """.trimIndent(),
            )
            database.execSQL(
                """
                INSERT INTO transactions (
                    id, merchant, amountMinor, description, currency,
                    category, account, occurredAt, syncState
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.trimIndent(),
                arrayOf(
                    "transaction-1",
                    "Warung",
                    -25_000L,
                    "Lunch",
                    "IDR",
                    "Dining",
                    "Cash",
                    1_754_000_000_000L,
                    "synced",
                ),
            )
        }

        openDatabase(
            version = 10,
            onUpgrade = { database, oldVersion, newVersion ->
                assertEquals(9, oldVersion)
                assertEquals(10, newVersion)
                LedgerDatabase.MIGRATION_9_10.migrate(database)
            },
        ) { database ->
            database.query("PRAGMA table_info(transactions)").use { cursor ->
                val nameIndex = cursor.getColumnIndexOrThrow("name")
                val typeIndex = cursor.getColumnIndexOrThrow("type")
                val notNullIndex = cursor.getColumnIndexOrThrow("notnull")
                val defaultIndex = cursor.getColumnIndexOrThrow("dflt_value")
                var found = false
                while (cursor.moveToNext()) {
                    if (cursor.getString(nameIndex) != "serverUpdatedAt") continue
                    found = true
                    assertEquals("TEXT", cursor.getString(typeIndex))
                    assertEquals(0, cursor.getInt(notNullIndex))
                    assertNull(cursor.getString(defaultIndex))
                }
                assertEquals(true, found)
            }

            database.query(
                "SELECT merchant, amountMinor, serverUpdatedAt FROM transactions WHERE id = ?",
                arrayOf("transaction-1"),
            ).use { cursor ->
                assertEquals(true, cursor.moveToFirst())
                assertEquals("Warung", cursor.getString(0))
                assertEquals(-25_000L, cursor.getLong(1))
                assertNull(cursor.getString(2))
            }

            database.execSQL(
                "UPDATE transactions SET serverUpdatedAt = ? WHERE id = ?",
                arrayOf("2026-08-14T00:00:00Z", "transaction-1"),
            )
            database.query(
                "SELECT serverUpdatedAt FROM transactions WHERE id = ?",
                arrayOf("transaction-1"),
            ).use { cursor ->
                assertEquals(true, cursor.moveToFirst())
                assertEquals("2026-08-14T00:00:00Z", cursor.getString(0))
            }
        }
    }

    @Test
    fun migration10To11PreservesSourceIdentityAndAddsUniquePlatformIdentity() {
        openDatabase(version = 10) { database ->
            database.execSQL(
                """
                CREATE TABLE ingestion_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    sourceRef TEXT NOT NULL,
                    packageName TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    amountIdr INTEGER,
                    merchant TEXT,
                    bank TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    occurredOn TEXT,
                    reviewRequired INTEGER NOT NULL,
                    receivedAt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    suspectedDuplicateOf INTEGER
                )
                """.trimIndent(),
            )
            database.execSQL(
                "CREATE UNIQUE INDEX index_ingestion_queue_sourceRef ON ingestion_queue(sourceRef)",
            )
            insertLegacyCapture(database, sourceRef = "legacy-content-ref")
        }

        openDatabase(
            version = 11,
            onUpgrade = { database, oldVersion, newVersion ->
                assertEquals(10, oldVersion)
                assertEquals(11, newVersion)
                LedgerDatabase.MIGRATION_10_11.migrate(database)
            },
        ) { database ->
            database.query(
                "SELECT sourceRef, platformIdentityRef FROM ingestion_queue WHERE id = 1",
            ).use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals("legacy-content-ref", cursor.getString(0))
                assertNull(cursor.getString(1))
            }

            var platformIndexIsUnique = false
            database.query("PRAGMA index_list('ingestion_queue')").use { cursor ->
                val nameIndex = cursor.getColumnIndexOrThrow("name")
                val uniqueIndex = cursor.getColumnIndexOrThrow("unique")
                while (cursor.moveToNext()) {
                    if (cursor.getString(nameIndex) == "index_ingestion_queue_platformIdentityRef") {
                        platformIndexIsUnique = cursor.getInt(uniqueIndex) == 1
                    }
                }
            }
            assertTrue(platformIndexIsUnique)

            database.execSQL(
                "UPDATE ingestion_queue SET platformIdentityRef = ? WHERE id = 1",
                arrayOf("android-notification-key-hash"),
            )
            insertLegacyCapture(database, sourceRef = "another-content-ref")
            val duplicateIdentityRejected = runCatching {
                database.execSQL(
                    "UPDATE ingestion_queue SET platformIdentityRef = ? WHERE sourceRef = ?",
                    arrayOf("android-notification-key-hash", "another-content-ref"),
                )
            }.isFailure
            assertTrue(duplicateIdentityRejected)
        }
    }

    @Test
    fun migration11To12AddsTransferMetadataWithoutChangingExistingRows() {
        openDatabase(version = 11) { database ->
            database.execSQL(
                """
                CREATE TABLE transactions (
                    id TEXT NOT NULL PRIMARY KEY,
                    merchant TEXT NOT NULL,
                    amountMinor INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    category TEXT NOT NULL,
                    account TEXT NOT NULL,
                    occurredAt INTEGER NOT NULL,
                    syncState TEXT NOT NULL,
                    serverUpdatedAt TEXT
                )
                """.trimIndent(),
            )
            database.execSQL(
                "INSERT INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                arrayOf("legacy-transfer", "Mandiri", -500_000L, "Transfer", "IDR", "Transfer", "Mandiri", 1_755_000_000_000L, "synced", null),
            )
        }

        openDatabase(
            version = 12,
            onUpgrade = { database, oldVersion, newVersion ->
                assertEquals(11, oldVersion)
                assertEquals(12, newVersion)
                LedgerDatabase.MIGRATION_11_12.migrate(database)
            },
        ) { database ->
            database.query(
                "SELECT amountMinor, kind, ledgerRole, transferBundleId, transferLeg FROM transactions WHERE id = ?",
                arrayOf("legacy-transfer"),
            ).use { cursor ->
                assertTrue(cursor.moveToFirst())
                assertEquals(-500_000L, cursor.getLong(0))
                assertEquals("expense", cursor.getString(1))
                assertEquals("ordinary", cursor.getString(2))
                assertNull(cursor.getString(3))
                assertNull(cursor.getString(4))
            }
        }
    }

    private fun insertLegacyCapture(database: SupportSQLiteDatabase, sourceRef: String) {
        database.execSQL(
            """
            INSERT INTO ingestion_queue (
                sourceRef, packageName, title, body, amountIdr, merchant, bank,
                direction, occurredOn, reviewRequired, receivedAt, status,
                suspectedDuplicateOf
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.trimIndent(),
            arrayOf(
                sourceRef,
                "id.bmri.livin",
                "Livin' by Mandiri",
                "Pembayaran Rp25.000 di TOKO",
                25_000L,
                "TOKO",
                "LIVIN_MANDIRI",
                "DEBIT",
                "2026-08-14",
                0,
                1_755_126_000_000L,
                "pending",
                null,
            ),
        )
    }

    private fun openDatabase(
        version: Int,
        onUpgrade: (SupportSQLiteDatabase, Int, Int) -> Unit = { _, _, _ -> },
        block: (SupportSQLiteDatabase) -> Unit,
    ) {
        var created = false
        val helper = FrameworkSQLiteOpenHelperFactory().create(
            SupportSQLiteOpenHelper.Configuration.builder(context)
                .name(databaseName)
                .callback(
                    object : SupportSQLiteOpenHelper.Callback(version) {
                        override fun onCreate(database: SupportSQLiteDatabase) {
                            created = true
                            block(database)
                        }

                        override fun onUpgrade(
                            database: SupportSQLiteDatabase,
                            oldVersion: Int,
                            newVersion: Int,
                        ) = onUpgrade(database, oldVersion, newVersion)
                    },
                )
                .build(),
        )
        try {
            val database = helper.writableDatabase
            if (!created && version > 9) block(database)
        } finally {
            helper.close()
        }
    }
}
