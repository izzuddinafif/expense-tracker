package com.afif.expensetracker.data

import androidx.room.Entity
import androidx.room.PrimaryKey

/** Durable position in one server-authoritative incremental feed. */
@Entity(tableName = "sync_checkpoints")
data class SyncCheckpoint(
    @PrimaryKey val feed: String,
    val cursor: String,
    val updatedAt: Long = System.currentTimeMillis(),
)

/** Single-process lease shared by immediate and periodic sync workers. */
@Entity(tableName = "sync_run_lock")
data class SyncRunLock(
    @PrimaryKey val id: Int = 1,
    val ownerToken: String? = null,
    val generation: Long = 0,
    val leaseExpiresAt: Long = 0,
)
