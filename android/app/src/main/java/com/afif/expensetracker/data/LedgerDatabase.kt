package com.afif.expensetracker.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [
        NotificationRecord::class,
        TransactionEntity::class,
        SyncOperation::class,
        SyncCheckpoint::class,
        SyncRunLock::class,
    ],
    version = 8,
    exportSchema = false,
)
abstract class LedgerDatabase : RoomDatabase() {
    abstract fun notificationDao(): NotificationDao
    abstract fun transactionDao(): TransactionDao
    abstract fun syncDao(): SyncDao
    companion object {
        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE transactions ADD COLUMN description TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE transactions ADD COLUMN account TEXT NOT NULL DEFAULT ''")
            }
        }
        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_operations ADD COLUMN lastAttemptAt INTEGER")
                db.execSQL("ALTER TABLE sync_operations ADD COLUMN lastError TEXT")
            }
        }
        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE sync_operations ADD COLUMN claimToken TEXT")
                db.execSQL("ALTER TABLE sync_operations ADD COLUMN claimGeneration INTEGER NOT NULL DEFAULT 0")
            }
        }
        private val MIGRATION_6_7 = object : Migration(6, 7) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("CREATE TABLE IF NOT EXISTS sync_checkpoints (feed TEXT NOT NULL, cursor TEXT NOT NULL, updatedAt INTEGER NOT NULL, PRIMARY KEY(feed))")
                db.execSQL("CREATE TABLE IF NOT EXISTS sync_run_lock (id INTEGER NOT NULL, ownerToken TEXT, generation INTEGER NOT NULL, leaseExpiresAt INTEGER NOT NULL, PRIMARY KEY(id))")
                db.execSQL("INSERT OR IGNORE INTO sync_run_lock (id, ownerToken, generation, leaseExpiresAt) VALUES (1, NULL, 0, 0)")
            }
        }
        private val MIGRATION_7_8 = object : Migration(7, 8) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE ingestion_queue ADD COLUMN suspectedDuplicateOf INTEGER")
            }
        }
        @Volatile private var instance: LedgerDatabase? = null
        fun get(context: Context): LedgerDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(
                context,
                LedgerDatabase::class.java,
                "ledgerly.db",
            ).addMigrations(MIGRATION_3_4, MIGRATION_4_5, MIGRATION_5_6, MIGRATION_6_7, MIGRATION_7_8).build().also { instance = it }
        }
    }
}
