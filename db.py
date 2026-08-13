import asyncio
from functools import wraps
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from models import ExpenseEntry, EmailTransaction, IncomeEntry, UserRecord

log = logging.getLogger(__name__)


class TransactionConflictError(ValueError):
    """Raised when a client edits a transaction from an obsolete revision."""


def _serialize_write(method):
    """Serialize writes sharing aiosqlite's single connection.

    aiosqlite serializes work submitted to its worker thread, but a coroutine
    can still interleave between BEGIN/commit calls. Keeping the critical
    section at the Database boundary prevents competing transactions from
    corrupting the connection's implicit transaction state.
    """
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        async with self._write_lock:
            return await method(self, *args, **kwargs)

    return guarded


def _canonical_occurred_on(value: str) -> str:
    """Validate an ISO calendar date and store it in canonical YYYY-MM-DD form."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("occurred_on is required")
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as exc:
        raise ValueError("occurred_on must be an ISO date") from exc


def _get_fernet() -> Fernet | None:
    """Return the configured Fernet instance, rejecting malformed keys."""
    key = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except Exception as exc:
        raise ValueError("Invalid TOKEN_ENCRYPTION_KEY") from exc


def _encrypt_token(token: str, fernet: Fernet | None) -> str:
    """Encrypt a token; new non-empty values require an encryption key."""
    if not token:
        return token
    if fernet is None:
        raise ValueError(
            "TOKEN_ENCRYPTION_KEY is required before storing a Notion token"
        )
    return "enc:" + fernet.encrypt(token.encode()).decode()


def _decrypt_token(stored: str, fernet: Fernet | None) -> str:
    """Decrypt a token from storage. Handles both encrypted and legacy plaintext."""
    if not stored:
        return stored
    if stored.startswith("enc:"):
        if fernet is None:
            raise ValueError(
                "Encrypted Notion token cannot be read without TOKEN_ENCRYPTION_KEY"
            )
        try:
            return fernet.decrypt(stored[4:].encode()).decode()
        except InvalidToken as exc:
            raise ValueError(
                "Encrypted Notion token cannot be decrypted with TOKEN_ENCRYPTION_KEY"
            ) from exc
    return stored  # legacy plaintext


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._ops_conn: aiosqlite.Connection | None = None
        self._ops_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._fernet = _get_fernet()
        if self._fernet:
            log.info("Token encryption enabled (TOKEN_ENCRYPTION_KEY set)")
        else:
            log.warning(
                "TOKEN_ENCRYPTION_KEY not set — legacy plaintext tokens remain readable, "
                "but new non-empty tokens cannot be stored"
            )

    def _row_to_user(self, row) -> UserRecord:
        """Build UserRecord from a DB row, decrypting the token."""
        return UserRecord(
            telegram_id=row["telegram_id"],
            owner_name=row["owner_name"],
            notion_token=_decrypt_token(row["notion_token"], self._fernet),
            expenses_ds=row["expenses_ds"],
            subcategories_ds=row["subcategories_ds"],
            accounts_ds=row["accounts_ds"],
            months_ds=row["months_ds"],
            years_ds=row["years_ds"],
            recurring_ds=row["recurring_ds"],
            assets_ds=row["assets_ds"],
            income_ds=row["income_ds"],
            income_subcategories_ds=row["income_subcategories_ds"],
            income_months_ds=row["income_months_ds"],
            income_years_ds=row["income_years_ds"],
            budget_ds=row["budget_ds"],
            categories_ds=row["categories_ds"],
            setup_step=row["setup_step"],
        )

    @classmethod
    async def connect(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        db: Database | None = None
        try:
            conn.row_factory = aiosqlite.Row
            db = cls(conn)
            await db._init()
            await db._migrate_schema()
            await db._migrate_from_json(path, conn)
            await db._migrate_legacy_notion_tokens()
            if path != ":memory:":
                db._ops_conn = await aiosqlite.connect(path)
                db._ops_conn.row_factory = aiosqlite.Row
                await db._ops_conn.execute("PRAGMA busy_timeout = 5000")
                await db._ops_conn.execute("PRAGMA journal_mode=WAL")
            return db
        except Exception:
            if db is not None:
                await db.close()
            else:
                await conn.close()
            raise

    async def close(self) -> None:
        if self._ops_conn is not None:
            await self._ops_conn.close()
        await self._conn.close()

    async def _migrate_legacy_notion_tokens(self) -> None:
        """Encrypt pre-existing plaintext tokens once a key is configured.

        Rows already prefixed with ``enc:`` are left untouched, making startup
        safe to repeat.  The update predicate also avoids overwriting a value
        that a concurrent writer has already migrated.
        """
        if self._fernet is None:
            return
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT telegram_id,notion_token FROM users "
                "WHERE notion_token<>'' AND notion_token NOT LIKE 'enc:%'"
            )
            legacy_rows = await cur.fetchall()
            for row in legacy_rows:
                await self._conn.execute(
                    "UPDATE users SET notion_token=? "
                    "WHERE telegram_id=? AND notion_token=?",
                    (
                        _encrypt_token(row["notion_token"], self._fernet),
                        row["telegram_id"],
                        row["notion_token"],
                    ),
                )
            await self._conn.commit()
            if legacy_rows:
                log.info("Migrated %d legacy Notion token(s) to encrypted storage", len(legacy_rows))
        except Exception:
            await self._conn.rollback()
            raise

    # ── Schema ──────────────────────────────────────────────────────────────────

    async def _init(self) -> None:
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                uid          TEXT PRIMARY KEY,
                sender       TEXT NOT NULL,
                processed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_expenses (
                user_id    INTEGER PRIMARY KEY,
                entry_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_email_expenses (
                user_id  INTEGER PRIMARY KEY,
                tx_json  TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_debit_queue (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                tx_json    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_income (
                user_id    INTEGER PRIMARY KEY,
                entry_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversation_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user
                ON conversation_history(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_debit_user
                ON pending_debit_queue(user_id);

            CREATE TABLE IF NOT EXISTS email_account_owners (
                account_pattern TEXT PRIMARY KEY,
                telegram_id    INTEGER NOT NULL,
                created_at     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_recurring (
                user_id            INTEGER PRIMARY KEY,
                entry_json         TEXT NOT NULL,
                recurring_page_url TEXT,
                uid                TEXT NOT NULL,
                sender             TEXT NOT NULL,
                created_at         TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_email_owner
                ON email_account_owners(telegram_id);

            CREATE TABLE IF NOT EXISTS pending_since (
                user_id    INTEGER PRIMARY KEY,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_undo (
                user_id      INTEGER PRIMARY KEY,
                page_id      TEXT NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                amount       REAL NOT NULL DEFAULT 0,
                date         TEXT NOT NULL DEFAULT '',
                subcat       TEXT NOT NULL DEFAULT '',
                merchant     TEXT NOT NULL DEFAULT '',
                created_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_saved_pages (
                user_id       INTEGER PRIMARY KEY,
                page_id       TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                amount        REAL NOT NULL DEFAULT 0,
                date          TEXT NOT NULL DEFAULT '',
                subcat        TEXT NOT NULL DEFAULT '',
                merchant      TEXT NOT NULL DEFAULT '',
                timestamp     REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS debit_merchant_cache (
                user_id    INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, amount)
            );

            CREATE TABLE IF NOT EXISTS merchant_patterns (
                user_id       INTEGER NOT NULL,
                merchant      TEXT NOT NULL DEFAULT "",
                subcategory   TEXT NOT NULL DEFAULT "",
                account       TEXT NOT NULL DEFAULT "",
                amount_bucket INTEGER NOT NULL,
                count         INTEGER NOT NULL DEFAULT 1,
                last_seen     TEXT NOT NULL,
                PRIMARY KEY (user_id, merchant, amount_bucket)
            );

            CREATE TABLE IF NOT EXISTS users (
                telegram_id              INTEGER PRIMARY KEY,
                owner_name               TEXT NOT NULL,
                notion_token             TEXT NOT NULL,
                expenses_ds              TEXT,
                subcategories_ds         TEXT,
                accounts_ds              TEXT,
                months_ds                TEXT,
                years_ds                 TEXT,
                recurring_ds             TEXT,
                assets_ds                TEXT,
                income_ds                TEXT,
                income_subcategories_ds  TEXT,
                income_months_ds         TEXT,
                income_years_ds          TEXT,
                budget_ds                TEXT,
                categories_ds            TEXT,
                setup_step               TEXT NOT NULL DEFAULT 'start',
                created_at               TEXT NOT NULL,
                updated_at               TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
        """)

    async def _migrate_schema(self) -> None:
        """Add new columns to existing tables for backward compatibility."""
        # Versioned ledger migration.  Existing workflow tables are deliberately
        # left intact; this migration only adds the durable local source of truth.
        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 1")
        if await cur.fetchone() is None:
            await self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('expense', 'income', 'transfer')),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'voided')),
                    amount_idr INTEGER NOT NULL CHECK (amount_idr > 0),
                    occurred_on TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    merchant TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    subcategory TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    source_ref TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    notion_page_id TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_source_ref
                    ON transactions(user_id, source, source_ref) WHERE source_ref IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_transactions_status_date
                    ON transactions(status, occurred_on);
                CREATE TABLE IF NOT EXISTS transaction_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transaction_events_tx
                    ON transaction_events(transaction_id, created_at);
                CREATE TABLE IF NOT EXISTS sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    operation TEXT NOT NULL CHECK (operation IN ('upsert', 'archive')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_outbox_open
                    ON sync_outbox(transaction_id, operation) WHERE completed_at IS NULL;
            """)
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 2")
        if await cur.fetchone() is None:
            columns = await self._conn.execute("PRAGMA table_info(transactions)")
            names = {row["name"] for row in await columns.fetchall()}
            if "recurring_page_id" not in names:
                await self._conn.execute(
                    "ALTER TABLE transactions ADD COLUMN recurring_page_id TEXT"
                )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 3")
        if await cur.fetchone() is None:
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS operational_state ("
                "name TEXT PRIMARY KEY,"
                "last_attempt_at TEXT,"
                "last_success_at TEXT,"
                "last_error TEXT,"
                "metadata_json TEXT NOT NULL DEFAULT '{}',"
                "updated_at TEXT NOT NULL)"
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (3, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 4")
        if await cur.fetchone() is None:
            columns = await self._conn.execute("PRAGMA table_info(operational_state)")
            names = {row["name"] for row in await columns.fetchall()}
            additions = {
                "started_at": "TEXT",
                "last_heartbeat_at": "TEXT",
                "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, definition in additions.items():
                if name not in names:
                    await self._conn.execute(
                        f"ALTER TABLE operational_state ADD COLUMN {name} {definition}"
                    )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (4, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 5")
        if await cur.fetchone() is None:
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS email_processing_failures (
                    uid TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL
                        CHECK (status IN ('retrying', 'degraded', 'terminal')),
                    first_failed_at TEXT NOT NULL,
                    last_failed_at TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    terminal_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_email_failures_status
                    ON email_processing_failures(status, last_failed_at);
                """
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (5, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 6")
        if await cur.fetchone() is None:
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS monthly_budgets (
                    user_id INTEGER NOT NULL,
                    month TEXT NOT NULL,
                    category TEXT NOT NULL COLLATE NOCASE,
                    amount_idr INTEGER NOT NULL CHECK (amount_idr > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, month, category)
                );
                CREATE INDEX IF NOT EXISTS idx_monthly_budgets_user_month
                    ON monthly_budgets(user_id, month);
                """
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (6, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 7")
        if await cur.fetchone() is None:
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS notion_cache_snapshots ("
                "user_id INTEGER PRIMARY KEY,"
                "cache_json TEXT NOT NULL,"
                "refreshed_at TEXT NOT NULL)"
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (7, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 8")
        if await cur.fetchone() is None:
            columns = await self._conn.execute("PRAGMA table_info(sync_outbox)")
            names = {row["name"] for row in await columns.fetchall()}
            if "revision" not in names:
                await self._conn.execute(
                    "ALTER TABLE sync_outbox ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
                )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (8, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        # Add merchant column to user_undo if missing
        try:
            await self._conn.execute("SELECT merchant FROM user_undo LIMIT 1")
        except Exception:
            log.info("Migrating: adding merchant column to user_undo")
            await self._conn.execute("ALTER TABLE user_undo ADD COLUMN merchant TEXT NOT NULL DEFAULT ''")
            await self._conn.commit()

        # Add merchant column to email_saved_pages if missing
        try:
            await self._conn.execute("SELECT merchant FROM email_saved_pages LIMIT 1")
        except Exception:
            log.info("Migrating: adding merchant column to email_saved_pages")
            await self._conn.execute("ALTER TABLE email_saved_pages ADD COLUMN merchant TEXT NOT NULL DEFAULT ''")
            await self._conn.commit()

    @staticmethod
    async def _migrate_from_json(path: str, conn: aiosqlite.Connection) -> None:
        json_path = Path(path).parent / "processed_emails.json"
        if not json_path.exists():
            return

        log.info(f"Migrating {json_path} → SQLite...")
        try:
            data = json.loads(json_path.read_text())
            now = datetime.now(timezone.utc).isoformat()
            if isinstance(data, list):
                entries = [(uid, "", now) for uid in data]
            else:
                entries = [(uid, "", ts or now) for uid, ts in data.items()]

            await conn.executemany(
                "INSERT OR IGNORE INTO processed_emails (uid, sender, processed_at) VALUES (?, ?, ?)",
                entries,
            )
            await conn.commit()
            json_path.rename(json_path.with_suffix(".json.migrated"))
            log.info(f"Migrated {len(entries)} entries, renamed to .migrated")
        except Exception as e:
            log.warning(f"Migration from {json_path} failed: {e}")

    @_serialize_write
    async def migrate_from_env(self, notion_token: str, users: dict[int, str]) -> None:
        """Pre-populate users table from env vars for backward compatibility."""
        if not notion_token or not users:
            return
        cur = await self._conn.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        if row[0] > 0:
            return  # users already exist, skip

        log.info("Migrating env vars → users table...")
        now = datetime.now(timezone.utc).isoformat()
        for uid, name in users.items():
            existing = await self.get_user(uid)
            if existing:
                continue
            # Token saved but setup_step='migrated' — user must run /setup to discover databases
            encrypted_token = _encrypt_token(notion_token, self._fernet)
            await self._conn.execute(
                "INSERT INTO users (telegram_id, owner_name, notion_token, setup_step, created_at, updated_at) "
                "VALUES (?, ?, ?, 'migrated', ?, ?)",
                (uid, name, encrypted_token, now, now),
            )
        await self._conn.commit()
        log.info(f"Migrated {len(users)} user(s) from env vars — run /setup to discover databases")

    # ── Processed emails ────────────────────────────────────────────────────────

    async def is_processed(self, uid: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE uid = ?", (uid,)
        )
        return await cur.fetchone() is not None

    @_serialize_write
    async def mark_processed(self, uid: str, sender: str) -> None:
        """Atomically mark success and clear any earlier per-UID failure."""
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                "INSERT OR REPLACE INTO processed_emails "
                "(uid, sender, processed_at) VALUES (?, ?, ?)",
                (uid, sender, datetime.now(timezone.utc).isoformat()),
            )
            await self._conn.execute(
                "DELETE FROM email_processing_failures WHERE uid=?", (uid,)
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def mark_rejected(self, uid: str, sender: str, reason: str) -> None:
        """Exclude a deterministic security reject while retaining its audit trail."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                "INSERT OR REPLACE INTO processed_emails (uid, sender, processed_at) VALUES (?, ?, ?)",
                (uid, sender, now),
            )
            await self._conn.execute(
                "INSERT INTO email_processing_failures "
                "(uid,sender,attempt_count,status,first_failed_at,last_failed_at,last_error,terminal_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET sender=excluded.sender,status='terminal',"
                "last_failed_at=excluded.last_failed_at,last_error=excluded.last_error,terminal_at=excluded.terminal_at",
                (uid, sender, 1, "terminal", now, now, reason[:1000], now),
            )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def get_all_processed_uids(self) -> set[str]:
        cur = await self._conn.execute("SELECT uid FROM processed_emails")
        rows = await cur.fetchall()
        return {row["uid"] for row in rows}

    @_serialize_write
    async def record_email_processing_failure(
        self,
        uid: str,
        sender: str,
        error: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a retryable email failure and return its updated state."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        current_iso = current.isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM email_processing_failures WHERE uid=?", (uid,)
            )
            existing = await cur.fetchone()
            attempts = (existing["attempt_count"] if existing else 0) + 1
            first_failed_at = (
                existing["first_failed_at"] if existing else current_iso
            )
            first = datetime.fromisoformat(first_failed_at.replace("Z", "+00:00"))
            if first.tzinfo is None:
                first = first.replace(tzinfo=timezone.utc)
            terminal = (
                (existing and existing["status"] == "terminal")
                or attempts >= 8
                or current - first.astimezone(timezone.utc) >= timedelta(hours=24)
            )
            status = "terminal" if terminal else (
                "degraded" if attempts >= 3 else "retrying"
            )
            terminal_at = (
                existing["terminal_at"]
                if existing and existing["terminal_at"]
                else current_iso if terminal else None
            )
            await self._conn.execute(
                "INSERT INTO email_processing_failures "
                "(uid,sender,attempt_count,status,first_failed_at,last_failed_at,"
                "last_error,terminal_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET sender=excluded.sender,"
                "attempt_count=excluded.attempt_count,status=excluded.status,"
                "last_failed_at=excluded.last_failed_at,"
                "last_error=excluded.last_error,terminal_at=excluded.terminal_at",
                (
                    uid,
                    sender,
                    attempts,
                    status,
                    first_failed_at,
                    current_iso,
                    error[:1000],
                    terminal_at,
                ),
            )
            await self._conn.commit()
            cur = await self._conn.execute(
                "SELECT * FROM email_processing_failures WHERE uid=?", (uid,)
            )
            return dict(await cur.fetchone())
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def get_email_excluded_uids(
        self, *, now: datetime | None = None
    ) -> set[str]:
        """Return successful and terminal UIDs, terminalizing 24-hour failures."""
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = (current - timedelta(hours=24)).isoformat()
        await self._conn.execute(
            "UPDATE email_processing_failures SET status='terminal',terminal_at=? "
            "WHERE status!='terminal' AND first_failed_at<=?",
            (current.isoformat(), cutoff),
        )
        await self._conn.commit()
        cur = await self._conn.execute(
            "SELECT uid FROM processed_emails "
            "UNION SELECT uid FROM email_processing_failures WHERE status='terminal'"
        )
        return {row["uid"] for row in await cur.fetchall()}

    @_serialize_write
    async def clear_email_processing_failure(self, uid: str) -> bool:
        """Allow an operator to retry a degraded or terminal email UID."""
        cur = await self._conn.execute(
            "DELETE FROM email_processing_failures WHERE uid=?", (uid,)
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def get_email_failure_summary(self) -> dict[str, int]:
        cur = await self._conn.execute(
            "SELECT "
            "SUM(CASE WHEN status='retrying' THEN 1 ELSE 0 END) AS retrying,"
            "SUM(CASE WHEN status='degraded' THEN 1 ELSE 0 END) AS degraded,"
            "SUM(CASE WHEN status='terminal' THEN 1 ELSE 0 END) AS terminal "
            "FROM email_processing_failures"
        )
        row = await cur.fetchone()
        return {
            "retrying": row["retrying"] or 0,
            "degraded": row["degraded"] or 0,
            "terminal": row["terminal"] or 0,
        }

    async def list_email_processing_failures(
        self, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return recent auditable email failures for authenticated diagnostics."""
        limit = max(1, min(limit, 100))
        cur = await self._conn.execute(
            "SELECT uid,sender,attempt_count,status,first_failed_at,last_failed_at,"
            "last_error,terminal_at FROM email_processing_failures "
            "ORDER BY CASE status WHEN 'terminal' THEN 0 WHEN 'degraded' THEN 1 "
            "ELSE 2 END,last_failed_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    @_serialize_write
    async def prune_processed(self, days: int = 90) -> int:
        cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM processed_emails WHERE processed_at < ?", (cutoff_dt,)
        )
        await self._conn.commit()
        return cur.rowcount

    # ── Pending expense (one per user) ──────────────────────────────────────────

    @_serialize_write
    async def set_pending_expense(self, user_id: int, entry: ExpenseEntry) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_expenses (user_id, entry_json, created_at) VALUES (?, ?, ?)",
            (user_id, entry.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def get_pending_expense(self, user_id: int) -> ExpenseEntry | None:
        cur = await self._conn.execute(
            "SELECT entry_json FROM pending_expenses WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return ExpenseEntry.model_validate_json(row["entry_json"])

    @_serialize_write
    async def clear_pending_expense(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_expenses WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    @_serialize_write
    async def _confirm_pending(self, user_id: int, *, kind: str, source: str,
                               source_ref: str | None = None,
                               recurring_page_id: str | None = None) -> str | None:
        """Atomically promote a pending entry into the local ledger.

        The pending row is removed, an audit event is appended, and one open
        Notion upsert is queued in the same ``BEGIN IMMEDIATE`` transaction.
        Repeating a confirmation with the same source reference is idempotent.
        """
        table = "pending_expenses" if kind == "expense" else "pending_income"
        json_col = "entry_json"
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                f"SELECT {json_col} FROM {table} WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            if row is None:
                if source_ref is not None:
                    cur = await self._conn.execute(
                        "SELECT id FROM transactions WHERE user_id = ? AND source = ? AND source_ref = ?",
                        (user_id, source, source_ref),
                    )
                    existing = await cur.fetchone()
                    if existing is not None:
                        await self._conn.rollback()
                        return existing["id"]
                await self._conn.rollback()
                return None
            entry = (ExpenseEntry if kind == "expense" else IncomeEntry).model_validate_json(row[json_col])
            if not float(entry.amount).is_integer():
                raise ValueError("IDR ledger amounts must be whole rupiah")
            amount_idr = int(entry.amount)
            occurred_on = _canonical_occurred_on(entry.date)
            now = datetime.now(timezone.utc).isoformat()
            tx_id: str | None = None
            if source_ref is not None:
                cur = await self._conn.execute(
                    "SELECT id, status FROM transactions WHERE user_id = ? AND source = ? AND source_ref = ?",
                    (user_id, source, source_ref),
                )
                existing = await cur.fetchone()
                if existing is not None:
                    tx_id = existing["id"]
                    if existing["status"] != "confirmed":
                        await self._conn.execute(
                            "UPDATE transactions SET status='confirmed', occurred_on=?, confirmed_at=?, updated_at=? WHERE id=?",
                            (occurred_on, now, now, tx_id),
                        )
                    else:
                        await self._conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                        await self._conn.commit()
                        return tx_id
            if tx_id is None:
                tx_id = str(uuid.uuid4())
                await self._conn.execute(
                    "INSERT INTO transactions (id, user_id, kind, status, amount_idr, occurred_on, description, merchant, "
                    "category, subcategory, account, source, source_ref, recurring_page_id, "
                    "created_at, updated_at, confirmed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tx_id, user_id, kind, "confirmed", amount_idr, occurred_on,
                        entry.description, getattr(entry, "merchant", ""), "",
                        entry.subcategory, entry.account, source, source_ref, recurring_page_id,
                        now, now, now,
                    ),
                )
            await self._conn.execute(
                "INSERT INTO transaction_events(transaction_id, event_type, metadata_json, created_at) VALUES (?, 'confirmed', ?, ?)",
                (tx_id, entry.model_dump_json(), now),
            )
            await self._conn.execute(
                "INSERT OR IGNORE INTO sync_outbox(transaction_id, operation, created_at, updated_at) VALUES (?, 'upsert', ?, ?)",
                (tx_id, now, now),
            )
            await self._conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            if kind == "expense":
                await self._conn.execute("DELETE FROM pending_since WHERE user_id = ?", (user_id,))
            await self._conn.commit()
            return tx_id
        except Exception:
            await self._conn.rollback()
            raise

    async def confirm_pending_expense(self, user_id: int, *, source: str = "telegram_text",
                                      source_ref: str | None = None,
                                      recurring_page_id: str | None = None) -> str | None:
        return await self._confirm_pending(
            user_id,
            kind="expense",
            source=source,
            source_ref=source_ref,
            recurring_page_id=recurring_page_id,
        )

    async def confirm_pending_income(self, user_id: int, *, source: str = "telegram_text",
                                     source_ref: str | None = None) -> str | None:
        return await self._confirm_pending(user_id, kind="income", source=source, source_ref=source_ref)

    @_serialize_write
    async def mark_notion_sync_success(
        self, outbox_id: int | str, notion_page_id: str | None = None
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        if isinstance(outbox_id, str):
            cur = await self._conn.execute(
                "SELECT id FROM sync_outbox WHERE transaction_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
                (outbox_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await self._conn.rollback()
                return
            outbox_id = row["id"]
        try:
            # A stale worker response may still reveal the stable remote page
            # ID, which helps the newer revision upsert it rather than create a
            # duplicate. It must not, however, complete that newer revision.
            if notion_page_id:
                await self._conn.execute(
                    "UPDATE transactions SET notion_page_id=?, updated_at=? "
                    "WHERE id=(SELECT transaction_id FROM sync_outbox WHERE id=?)",
                    (notion_page_id, now, outbox_id),
                )
            cur = await self._conn.execute(
                "UPDATE sync_outbox SET completed_at=?, updated_at=?, last_error=NULL "
                "WHERE id=? AND completed_at IS NULL",
                (now, now, outbox_id),
            )
            if cur.rowcount:
                await self._conn.execute(
                    "INSERT INTO transaction_events(transaction_id, event_type, metadata_json, created_at) "
                    "SELECT transaction_id, 'synced', '{}', ? FROM sync_outbox WHERE id=?",
                    (now, outbox_id),
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def mark_notion_sync_failure(self, outbox_id: int | str, error: str,
                                       next_attempt_at: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        if isinstance(outbox_id, str):
            cur = await self._conn.execute(
                "SELECT id FROM sync_outbox WHERE transaction_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1",
                (outbox_id,),
            )
            row = await cur.fetchone()
            if row is None:
                await self._conn.rollback()
                return
            outbox_id = row["id"]
        try:
            cur = await self._conn.execute(
                "UPDATE sync_outbox SET attempt_count=attempt_count+1, last_error=?, "
                "next_attempt_at=?, updated_at=? WHERE id=? AND completed_at IS NULL",
                (error, next_attempt_at, now, outbox_id),
            )
            if cur.rowcount:
                await self._conn.execute(
                    "INSERT INTO transaction_events(transaction_id, event_type, metadata_json, created_at) "
                    "SELECT transaction_id, 'sync_failed', ?, ? FROM sync_outbox WHERE id=?",
                    (json.dumps({"error": error}), now, outbox_id),
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def list_due_notion_sync_jobs(
        self, now: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return incomplete Notion jobs due for this single-process worker."""
        limit = max(1, min(limit, 100))
        cur = await self._conn.execute(
            "SELECT o.id AS outbox_id,o.operation,o.revision AS outbox_revision,o.attempt_count,"
            "t.id AS transaction_id,t.user_id,t.kind,t.status,t.amount_idr,"
            "t.occurred_on,t.description,t.merchant,t.subcategory,t.account,"
            "t.notion_page_id,t.recurring_page_id "
            "FROM sync_outbox o JOIN transactions t ON t.id=o.transaction_id "
            "WHERE o.completed_at IS NULL "
            "AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=?) "
            "ORDER BY COALESCE(o.next_attempt_at,o.created_at),o.id LIMIT ?",
            (now, limit),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get_notion_sync_status(self, user_id: int) -> dict[str, Any]:
        """Summarize incomplete Notion outbox work for one user."""
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS pending_count,"
            "SUM(CASE WHEN o.last_error IS NOT NULL THEN 1 ELSE 0 END) AS failed_count,"
            "MIN(o.created_at) AS oldest_pending_at,"
            "MAX(o.attempt_count) AS max_attempt_count "
            "FROM sync_outbox o JOIN transactions t ON t.id=o.transaction_id "
            "WHERE t.user_id=? AND o.completed_at IS NULL",
            (user_id,),
        )
        summary = await cur.fetchone()
        errors = await self._conn.execute(
            "SELECT o.id AS outbox_id,t.id AS transaction_id,o.attempt_count,"
            "o.last_error,o.next_attempt_at "
            "FROM sync_outbox o JOIN transactions t ON t.id=o.transaction_id "
            "WHERE t.user_id=? AND o.completed_at IS NULL "
            "AND o.last_error IS NOT NULL ORDER BY o.updated_at DESC LIMIT 5",
            (user_id,),
        )
        return {
            "pending_count": summary["pending_count"],
            "failed_count": summary["failed_count"] or 0,
            "oldest_pending_at": summary["oldest_pending_at"],
            "max_attempt_count": summary["max_attempt_count"] or 0,
            "recent_errors": [dict(row) for row in await errors.fetchall()],
        }

    @_serialize_write
    async def retry_notion_sync(self, user_id: int) -> int:
        """Make failed jobs due now without creating duplicate outbox rows."""
        now = datetime.now(timezone.utc).isoformat()
        cur = await self._conn.execute(
            "UPDATE sync_outbox SET next_attempt_at=NULL,updated_at=? "
            "WHERE completed_at IS NULL AND last_error IS NOT NULL "
            "AND transaction_id IN (SELECT id FROM transactions WHERE user_id=?)",
            (now, user_id),
        )
        await self._conn.commit()
        return cur.rowcount

    @_serialize_write
    async def record_operational_state(
        self,
        name: str,
        *,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a named worker heartbeat without storing sensitive payloads."""
        name = name.strip()
        if not name:
            raise ValueError("Operational state name is required")
        now = datetime.now(timezone.utc).isoformat()
        conn = self._ops_conn or self._conn
        async with self._ops_lock:
            await conn.execute(
                "INSERT INTO operational_state "
                "(name,started_at,last_heartbeat_at,last_attempt_at,last_success_at,"
                "last_error,consecutive_failures,metadata_json,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "last_heartbeat_at=excluded.last_heartbeat_at,"
                "last_attempt_at=excluded.last_attempt_at,"
                "last_success_at=CASE WHEN ? THEN excluded.last_success_at "
                "ELSE operational_state.last_success_at END,"
                "last_error=excluded.last_error,"
                "consecutive_failures=CASE WHEN ? THEN 0 "
                "ELSE operational_state.consecutive_failures + 1 END,"
                "metadata_json=excluded.metadata_json,"
                "updated_at=excluded.updated_at",
                (
                    name,
                    now,
                    now,
                    now,
                    now if success else None,
                    None if success else (error or "unknown error")[:1000],
                    0 if success else 1,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    success,
                    success,
                ),
            )
            await conn.commit()

    @_serialize_write
    async def record_operational_heartbeat(
        self, name: str, *, metadata: dict[str, Any] | None = None
    ) -> None:
        """Record loop liveness without claiming an external operation succeeded."""
        name = name.strip()
        if not name:
            raise ValueError("Operational state name is required")
        now = datetime.now(timezone.utc).isoformat()
        conn = self._ops_conn or self._conn
        async with self._ops_lock:
            await conn.execute(
                "INSERT INTO operational_state "
                "(name,started_at,last_heartbeat_at,consecutive_failures,"
                "metadata_json,updated_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "last_heartbeat_at=excluded.last_heartbeat_at,"
                "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                (name, now, now, 0, json.dumps(metadata or {}), now),
            )
            await conn.commit()

    async def get_operational_health(self, user_id: int) -> dict[str, Any]:
        """Return durable worker and outbox state for diagnostics."""
        sync = await self.get_notion_sync_status(user_id)
        conn = self._ops_conn or self._conn
        cur = await conn.execute(
            "SELECT name,started_at,last_heartbeat_at,last_attempt_at,last_success_at,"
            "last_error,consecutive_failures,metadata_json "
            "FROM operational_state ORDER BY name"
        )
        workers: dict[str, Any] = {}
        for row in await cur.fetchall():
            workers[row["name"]] = {
                "last_attempt_at": row["last_attempt_at"],
                "last_success_at": row["last_success_at"],
                "last_error": row["last_error"],
                "started_at": row["started_at"],
                "last_heartbeat_at": row["last_heartbeat_at"],
                "consecutive_failures": row["consecutive_failures"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
        from operations import classify_operational_health

        return classify_operational_health(sync, workers)

    async def list_transactions(
        self,
        user_id: int,
        *,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return newest ledger rows for one user."""
        limit = max(1, min(limit, 200))
        params: list[Any] = [user_id]
        where = "user_id = ?"
        if status is not None:
            if status not in {"pending", "confirmed", "voided"}:
                raise ValueError("Invalid transaction status")
            where += " AND status = ?"
            params.append(status)
        params.append(limit)
        cur = await self._conn.execute(
            f"SELECT * FROM transactions WHERE {where} "
            "ORDER BY occurred_on DESC, created_at DESC LIMIT ?",
            params,
        )
        return [dict(row) for row in await cur.fetchall()]

    async def list_transaction_changes(
        self,
        user_id: int,
        *,
        limit: int = 50,
        after_updated_at: str | None = None,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return confirmed/voided ledger changes using a keyset cursor.

        Rows are ordered by ``(updated_at, id)`` so records sharing a timestamp
        remain deterministic. The extra row lets callers determine whether a
        subsequent page exists without OFFSET scans.
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        params: list[Any] = [user_id]
        where = "user_id=? AND status IN ('confirmed','voided')"
        if after_updated_at is not None or after_id is not None:
            if not after_updated_at or not after_id:
                raise ValueError("cursor is incomplete")
            where += " AND (updated_at > ? OR (updated_at = ? AND id > ?))"
            params.extend([after_updated_at, after_updated_at, after_id])
        params.append(limit + 1)
        cur = await self._conn.execute(
            f"SELECT * FROM transactions WHERE {where} "
            "ORDER BY updated_at ASC, id ASC LIMIT ?",
            params,
        )
        return [dict(row) for row in await cur.fetchall()]

    async def find_transaction_by_notion_page_id(
        self, user_id: int, notion_page_id: str
    ) -> dict[str, Any] | None:
        """Resolve a legacy page-based callback to its canonical ledger row."""
        normalized = notion_page_id.replace("-", "").strip()
        cur = await self._conn.execute(
            "SELECT * FROM transactions WHERE user_id=? "
            "AND replace(notion_page_id, '-', '')=? LIMIT 1",
            (user_id, normalized),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def find_transaction_by_id(
        self, user_id: int, transaction_id: str
    ) -> dict[str, Any] | None:
        cur = await self._conn.execute(
            "SELECT * FROM transactions WHERE user_id=? AND id=? LIMIT 1",
            (user_id, transaction_id),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    @_serialize_write
    async def create_ingested_transaction(
        self,
        user_id: int,
        *,
        kind: str,
        amount_idr: int,
        occurred_on: str,
        description: str,
        source_ref: str,
        merchant: str = "",
        category: str = "",
        subcategory: str = "",
        account: str = "",
        metadata: dict[str, Any] | None = None,
        source: str = "android_notification",
    ) -> tuple[dict[str, Any], bool]:
        """Create an idempotent pending ingested ledger row.

        Returns ``(row, created)``. Replaying the same source reference for the
        same user returns the existing row without appending another event.

        ``android_notification`` is the default ingestion source. ``manual``
        is reserved for intentional entries created by the Android UI; it uses
        the same source-ref idempotency boundary and is never suppressed at
        creation time. Cross-source rows are not merged because this payload
        carries no durable shared bank transaction identifier.
        """
        if kind not in {"expense", "income", "transfer"}:
            raise ValueError("Invalid transaction kind")
        if source not in {"android_notification", "manual"}:
            raise ValueError("Invalid transaction source")
        if isinstance(amount_idr, bool) or not isinstance(amount_idr, int) or amount_idr <= 0:
            raise ValueError("amount_idr must be a positive integer")
        occurred_on = _canonical_occurred_on(occurred_on)
        source_ref = source_ref.strip()
        if not source_ref:
            raise ValueError("source_ref is required")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM transactions "
                "WHERE user_id=? AND source=? AND source_ref=?",
                (user_id, source, source_ref),
            )
            existing = await cur.fetchone()
            if existing is not None:
                await self._conn.rollback()
                return dict(existing), False

            tx_id = str(uuid.uuid4())
            await self._conn.execute(
                "INSERT INTO transactions "
                "(id,user_id,kind,status,amount_idr,occurred_on,description,merchant,"
                "category,subcategory,account,source,source_ref,created_at,updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?)",
                (
                    tx_id, user_id, kind, amount_idr, occurred_on, description.strip(),
                    merchant.strip(), category.strip(), subcategory.strip(), account.strip(),
                    source, source_ref, now, now,
                ),
            )
            await self._conn.execute(
                "INSERT INTO transaction_events "
                "(transaction_id,event_type,metadata_json,created_at) VALUES (?, 'ingested', ?, ?)",
                (tx_id, json.dumps(metadata or {}, ensure_ascii=False), now),
            )
            await self._conn.commit()
            cur = await self._conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
            return dict(await cur.fetchone()), True
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def create_confirmed_external_transaction(
        self,
        user_id: int,
        *,
        kind: str,
        amount_idr: int,
        occurred_on: str,
        description: str,
        source: str,
        source_ref: str,
        merchant: str = "",
        category: str = "",
        subcategory: str = "",
        account: str = "",
        recurring_page_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically store an already-confirmed external record and queue sync."""
        if kind not in {"expense", "income"}:
            raise ValueError("External transaction kind must be expense or income")
        if isinstance(amount_idr, bool) or not isinstance(amount_idr, int) or amount_idr <= 0:
            raise ValueError("amount_idr must be a positive integer")
        occurred_on = _canonical_occurred_on(occurred_on)
        source = source.strip()
        source_ref = source_ref.strip()
        if not source or not source_ref:
            raise ValueError("source and source_ref are required")
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM transactions "
                "WHERE user_id=? AND source=? AND source_ref=?",
                (user_id, source, source_ref),
            )
            existing = await cur.fetchone()
            if existing is not None:
                await self._conn.rollback()
                return dict(existing), False
            tx_id = str(uuid.uuid4())
            await self._conn.execute(
                "INSERT INTO transactions "
                "(id,user_id,kind,status,amount_idr,occurred_on,description,"
                "merchant,category,subcategory,account,source,source_ref,"
                "recurring_page_id,created_at,updated_at,confirmed_at) "
                "VALUES (?,?,?,'confirmed',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx_id,
                    user_id,
                    kind,
                    amount_idr,
                    occurred_on,
                    description,
                    merchant,
                    category,
                    subcategory,
                    account,
                    source,
                    source_ref,
                    recurring_page_id,
                    now,
                    now,
                    now,
                ),
            )
            await self._conn.execute(
                "INSERT INTO transaction_events "
                "(transaction_id,event_type,metadata_json,created_at) "
                "VALUES (?,'confirmed_external',?,?)",
                (
                    tx_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            await self._conn.execute(
                "INSERT INTO sync_outbox "
                "(transaction_id,operation,created_at,updated_at) "
                "VALUES (?,'upsert',?,?)",
                (tx_id, now, now),
            )
            await self._conn.commit()
            cur = await self._conn.execute(
                "SELECT * FROM transactions WHERE id=?", (tx_id,)
            )
            return dict(await cur.fetchone()), True
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def confirm_transaction(
        self, user_id: int, transaction_id: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """Idempotently confirm an ingested transaction and queue Notion sync."""
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM transactions WHERE id=? AND user_id=?",
                (transaction_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                await self._conn.rollback()
                return None, False
            if row["kind"] == "transfer":
                raise ValueError("Transfer transactions are not supported by Notion sync")
            occurred_on = _canonical_occurred_on(row["occurred_on"])
            changed = (
                row["status"] != "confirmed"
                or row["occurred_on"] != occurred_on
            )
            if changed:
                await self._conn.execute(
                    "UPDATE transactions SET status='confirmed',occurred_on=?,"
                    "confirmed_at=COALESCE(confirmed_at,?),updated_at=? "
                    "WHERE id=? AND user_id=?",
                    (occurred_on, now, now, transaction_id, user_id),
                )
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) "
                    "VALUES (?, 'confirmed', '{}', ?)",
                    (transaction_id, now),
                )
            await self._conn.execute(
                "INSERT OR IGNORE INTO sync_outbox "
                "(transaction_id,operation,created_at,updated_at) VALUES (?, 'upsert', ?, ?)",
                (transaction_id, now, now),
            )
            await self._conn.commit()
            cur = await self._conn.execute(
                "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            )
            return dict(await cur.fetchone()), changed
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def update_transaction(
        self,
        user_id: int,
        transaction_id: str,
        changes: dict[str, Any],
        *,
        expected_updated_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically edit a ledger row and queue a Notion upsert.

        Only mutable ledger fields are accepted.  The operation is scoped by
        ``user_id`` and appends an audit event in the same SQLite transaction.
        An edit fences any open upsert and creates a new revision. A worker
        holding an older snapshot cannot acknowledge this newer edit.
        """
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty mapping")
        allowed = {
            "amount_idr", "occurred_on", "description", "merchant", "category",
            "subcategory", "account", "recurring_page_id",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Invalid transaction fields: {', '.join(sorted(unknown))}")
        if "amount_idr" in changes:
            amount = changes["amount_idr"]
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                raise ValueError("amount_idr must be a positive integer")
        if "occurred_on" in changes:
            changes["occurred_on"] = _canonical_occurred_on(changes["occurred_on"])
        if "recurring_page_id" in changes and changes["recurring_page_id"] is not None and not isinstance(
            changes["recurring_page_id"], str
        ):
            raise ValueError("recurring_page_id must be a string or null")
        for field in allowed - {"amount_idr", "occurred_on", "recurring_page_id"}:
            if field in changes and not isinstance(changes[field], str):
                raise ValueError(f"{field} must be a string")

        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM transactions WHERE id=? AND user_id=?",
                (transaction_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                await self._conn.rollback()
                return None, False
            if row["status"] != "confirmed":
                raise ValueError("Only confirmed transactions can be updated")
            if expected_updated_at is not None and expected_updated_at != row["updated_at"]:
                await self._conn.rollback()
                raise TransactionConflictError(
                    "Transaction changed on the server; reload it before editing"
                )

            normalized = {
                field: (
                    value.strip()
                    if isinstance(value, str) and field != "recurring_page_id"
                    else value
                )
                for field, value in changes.items()
            }
            if all(row[field] == value for field, value in normalized.items()):
                await self._conn.rollback()
                return dict(row), False

            assignments: list[str] = []
            params: list[Any] = []
            for field, value in normalized.items():
                assignments.append(f"{field}=?")
                params.append(value)
            assignments.append("updated_at=?")
            params.append(now)
            params.extend([transaction_id, user_id])
            await self._conn.execute(
                f"UPDATE transactions SET {', '.join(assignments)} WHERE id=? AND user_id=?",
                params,
            )
            await self._conn.execute(
                "INSERT INTO transaction_events(transaction_id,event_type,metadata_json,created_at) VALUES (?, 'edited', ?, ?)",
                (transaction_id, json.dumps(normalized, ensure_ascii=False, default=str), now),
            )
            await self._conn.execute(
                "UPDATE sync_outbox SET completed_at=?,updated_at=? WHERE transaction_id=? "
                "AND operation='upsert' AND completed_at IS NULL",
                (now, now, transaction_id),
            )
            await self._conn.execute(
                "INSERT INTO sync_outbox(transaction_id,operation,revision,created_at,updated_at) "
                "SELECT ?, 'upsert', COALESCE(MAX(revision), 0) + 1, ?, ? "
                "FROM sync_outbox WHERE transaction_id=? AND operation='upsert'",
                (transaction_id, now, now, transaction_id),
            )
            await self._conn.commit()
            cur = await self._conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,))
            return dict(await cur.fetchone()), True
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def void_transaction(
        self,
        user_id: int,
        transaction_id: str,
        *,
        expected_updated_at: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Atomically void a transaction and enqueue a Notion archive.

        Any open upsert is completed before the archive is inserted, preventing
        a worker from racing the void and recreating the remote page. Repeating
        the call is a no-op and does not append duplicate audit events.
        """
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "SELECT * FROM transactions WHERE id=? AND user_id=?",
                (transaction_id, user_id),
            )
            row = await cur.fetchone()
            if row is None:
                await self._conn.rollback()
                return None, False
            if row["status"] == "voided":
                await self._conn.rollback()
                return dict(row), False
            if expected_updated_at is not None and expected_updated_at != row["updated_at"]:
                await self._conn.rollback()
                raise TransactionConflictError(
                    "Transaction changed on the server; reload it before voiding"
                )

            await self._conn.execute(
                "UPDATE transactions SET status='voided',updated_at=? WHERE id=? AND user_id=?",
                (now, transaction_id, user_id),
            )
            await self._conn.execute(
                "INSERT INTO transaction_events(transaction_id,event_type,metadata_json,created_at) VALUES (?, 'voided', '{}', ?)",
                (transaction_id, now),
            )
            await self._conn.execute(
                "UPDATE sync_outbox SET completed_at=?,updated_at=? "
                "WHERE transaction_id=? AND operation='upsert' AND completed_at IS NULL",
                (now, now, transaction_id),
            )
            await self._conn.execute(
                "INSERT OR IGNORE INTO sync_outbox(transaction_id,operation,created_at,updated_at) VALUES (?, 'archive', ?, ?)",
                (transaction_id, now, now),
            )
            await self._conn.commit()
            cur = await self._conn.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,))
            return dict(await cur.fetchone()), True
        except Exception:
            await self._conn.rollback()
            raise

    # ── Pending income (one per user) ──────────────────────────────────────────

    @_serialize_write
    async def set_pending_income(self, user_id: int, entry: IncomeEntry) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_income (user_id, entry_json, created_at) VALUES (?, ?, ?)",
            (user_id, entry.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def get_pending_income(self, user_id: int) -> IncomeEntry | None:
        cur = await self._conn.execute(
            "SELECT entry_json FROM pending_income WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return IncomeEntry.model_validate_json(row["entry_json"])

    @_serialize_write
    async def clear_pending_income(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_income WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Pending email expense (current debit card follow-up) ────────────────────

    @_serialize_write
    async def set_pending_email_expense(self, user_id: int, tx: EmailTransaction) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_email_expenses (user_id, tx_json, created_at) VALUES (?, ?, ?)",
            (user_id, tx.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def get_pending_email_expense(self, user_id: int) -> EmailTransaction | None:
        cur = await self._conn.execute(
            "SELECT tx_json FROM pending_email_expenses WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return EmailTransaction.model_validate_json(row["tx_json"])

    @_serialize_write
    async def clear_pending_email_expense(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_email_expenses WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Debit queue (FIFO per user) ─────────────────────────────────────────────

    @_serialize_write
    async def push_debit(self, user_id: int, tx: EmailTransaction) -> None:
        await self._conn.execute(
            "INSERT INTO pending_debit_queue (user_id, tx_json, created_at) VALUES (?, ?, ?)",
            (user_id, tx.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    @_serialize_write
    async def pop_debit(self, user_id: int) -> EmailTransaction | None:
        """Pop the oldest queued debit tx for this user. Returns None if empty.
        Uses DELETE ... RETURNING to prevent SELECT-then-DELETE race."""
        cur = await self._conn.execute(
            "DELETE FROM pending_debit_queue WHERE id IN ("
            "SELECT id FROM pending_debit_queue WHERE user_id = ? ORDER BY id ASC LIMIT 1"
            ") RETURNING tx_json",
            (user_id,),
        )
        row = await cur.fetchone()
        await self._conn.commit()
        if row is None:
            return None
        return EmailTransaction.model_validate_json(row["tx_json"])

    async def debit_queue_depth(self, user_id: int) -> int:
        cur = await self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM pending_debit_queue WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        return row["cnt"] if row else 0

    @_serialize_write
    async def clear_debit_queue(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_debit_queue WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Debit merchant cache (auto-learned descriptions) ───────────────────────

    async def get_debit_merchant(self, user_id: int, amount: float) -> str | None:
        cur = await self._conn.execute(
            "SELECT description FROM debit_merchant_cache WHERE user_id = ? AND amount = ?",
            (user_id, int(round(amount))),
        )
        row = await cur.fetchone()
        return row["description"] if row else None

    @_serialize_write
    async def set_debit_merchant(self, user_id: int, amount: float, description: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO debit_merchant_cache (user_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
            (user_id, int(round(amount)), description, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    # ── Merchant patterns (amount-bucketed auto-detection) ─────────────────────

    @_serialize_write
    async def record_pattern(self, user_id: int, merchant: str, subcategory: str, account: str, amount: float, date: str) -> None:
        merchant = merchant.strip()
        if not merchant:
            return
        bucket = round(amount / 10000) * 10000
        cur = await self._conn.execute(
            "SELECT count FROM merchant_patterns WHERE user_id = ? AND merchant = ? AND amount_bucket = ?",
            (user_id, merchant, bucket),
        )
        row = await cur.fetchone()
        if row:
            await self._conn.execute(
                "UPDATE merchant_patterns SET count = count + 1, last_seen = ? WHERE user_id = ? AND merchant = ? AND amount_bucket = ?",
                (date, user_id, merchant, bucket),
            )
        else:
            await self._conn.execute(
                "INSERT INTO merchant_patterns (user_id, merchant, subcategory, account, amount_bucket, count, last_seen) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (user_id, merchant, subcategory, account, bucket, date),
            )
        await self._conn.commit()

    async def find_pattern(self, user_id: int, amount: float) -> dict | None:
        bucket = round(amount / 10000) * 10000
        cur = await self._conn.execute(
            "SELECT merchant, subcategory, account, count FROM merchant_patterns "
            "WHERE user_id = ? AND merchant <> '' AND amount_bucket BETWEEN ? AND ? "
            "ORDER BY count DESC LIMIT 1",
            (user_id, bucket - 10000, bucket + 10000),
        )
        row = await cur.fetchone()
        if row:
            return {"merchant": row["merchant"], "subcategory": row["subcategory"], "account": row["account"]}
        return None

    # ── Conversation history ────────────────────────────────────────────────────

    @_serialize_write
    async def append_history(self, user_id: int, role: str, content: str) -> None:
        await self._conn.execute(
            "INSERT INTO conversation_history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, datetime.now(timezone.utc).isoformat()),
        )
        # Keep at most 100 rows per user to prevent unbounded table growth
        await self._conn.execute(
            "DELETE FROM conversation_history WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM conversation_history WHERE user_id = ? ORDER BY id DESC LIMIT 100)",
            (user_id, user_id),
        )
        await self._conn.commit()

    async def get_history(self, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT role, content FROM ("
            "SELECT role, content, created_at, id FROM conversation_history "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ?"
            ") ORDER BY created_at ASC, id ASC",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    @_serialize_write
    async def clear_history(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM conversation_history WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Pending since (auto-confirm timestamps) ──────────────────────────────

    @_serialize_write
    async def set_pending_since(self, user_id: int, timestamp: float) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_since (user_id, created_at) VALUES (?, ?)",
            (user_id, timestamp),
        )
        await self._conn.commit()

    @_serialize_write
    async def clear_pending_since(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_since WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def get_all_pending_since(self) -> dict[int, float]:
        cur = await self._conn.execute("SELECT user_id, created_at FROM pending_since")
        rows = await cur.fetchall()
        return {row["user_id"]: row["created_at"] for row in rows}

    async def get_pending_since(self, user_id: int) -> float | None:
        cur = await self._conn.execute(
            "SELECT created_at FROM pending_since WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["created_at"] if row else None

    # ── User undo (persist last_saved_page across restarts) ───────────────────

    @_serialize_write
    async def set_user_undo(self, user_id: int, page_id: str, description: str = "", amount: float = 0, date: str = "", subcat: str = "", merchant: str = "") -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO user_undo (user_id, page_id, description, amount, date, subcat, merchant, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, page_id, description, amount, date, subcat, merchant, datetime.now().timestamp()),
        )
        await self._conn.commit()

    async def get_user_undo(self, user_id: int) -> dict | None:
        cur = await self._conn.execute(
            "SELECT page_id, description, amount, date, subcat, merchant, created_at FROM user_undo WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "page_id": row["page_id"],
            "description": row["description"],
            "amount": row["amount"],
            "date": row["date"],
            "subcat": row["subcat"],
            "merchant": row["merchant"],
            "created_at": row["created_at"],
        }

    @_serialize_write
    async def clear_user_undo(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM user_undo WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def get_all_user_undo(self) -> dict[int, str]:
        cur = await self._conn.execute(
            "SELECT user_id, page_id FROM user_undo"
        )
        rows = await cur.fetchall()
        return {row["user_id"]: row["page_id"] for row in rows}

    # ── Email saved pages (persist email_saved_pages across restarts) ─────────

    @_serialize_write
    async def set_email_saved_page(self, user_id: int, page_id: str, description: str, amount: float, date: str, subcat: str, timestamp: float, merchant: str = "") -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO email_saved_pages (user_id, page_id, description, amount, date, subcat, merchant, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, page_id, description, amount, date, subcat, merchant, timestamp),
        )
        await self._conn.commit()

    async def get_email_saved_page(self, user_id: int) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM email_saved_pages WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "page_id": row["page_id"],
            "description": row["description"],
            "amount": row["amount"],
            "date": row["date"],
            "subcat": row["subcat"],
            "merchant": row["merchant"],
            "timestamp": row["timestamp"],
        }

    @_serialize_write
    async def clear_email_saved_page(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM email_saved_pages WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    async def get_all_email_saved_pages(self) -> dict[int, dict]:
        cur = await self._conn.execute(
            "SELECT * FROM email_saved_pages"
        )
        rows = await cur.fetchall()
        result: dict[int, dict] = {}
        for row in rows:
            result[row["user_id"]] = {
                "page_id": row["page_id"],
                "description": row["description"],
                "amount": row["amount"],
                "date": row["date"],
                "subcat": row["subcat"],
                "merchant": row["merchant"],
                "timestamp": row["timestamp"],
            }
        return result

    # ── Email account owners (multi-user email watcher) ───────────────────────

    @_serialize_write
    async def set_email_account_owner(self, account_pattern: str, telegram_id: int) -> None:
        cur = await self._conn.execute(
            "SELECT telegram_id FROM email_account_owners WHERE account_pattern = ?",
            (account_pattern,),
        )
        existing = await cur.fetchone()
        if existing is not None and existing["telegram_id"] != telegram_id:
            raise ValueError("email account is already linked to another user")
        await self._conn.execute(
            "INSERT INTO email_account_owners (account_pattern, telegram_id, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(account_pattern) DO UPDATE SET telegram_id=excluded.telegram_id, created_at=excluded.created_at",
            (account_pattern, telegram_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    @_serialize_write
    async def remove_email_account_owner(self, account_pattern: str) -> None:
        await self._conn.execute(
            "DELETE FROM email_account_owners WHERE account_pattern = ?", (account_pattern,)
        )
        await self._conn.commit()

    async def get_email_owner_for_account(self, account_name: str) -> int | None:
        """Fuzzy match an account name to a telegram_id. Exact match first, then substring."""
        cur = await self._conn.execute("SELECT account_pattern, telegram_id FROM email_account_owners")
        rows = await cur.fetchall()
        # exact match
        for row in rows:
            if row["account_pattern"].lower() == account_name.lower():
                return row["telegram_id"]
        # substring match: account_name contains pattern, or pattern contains account_name
        for row in rows:
            pat_lower = row["account_pattern"].lower()
            if pat_lower in account_name.lower() or account_name.lower() in pat_lower:
                return row["telegram_id"]
        return None

    async def get_all_email_account_owners(self) -> dict[str, int]:
        cur = await self._conn.execute("SELECT account_pattern, telegram_id FROM email_account_owners")
        rows = await cur.fetchall()
        return {row["account_pattern"]: row["telegram_id"] for row in rows}

    async def get_email_accounts_for_user(self, telegram_id: int) -> list[str]:
        cur = await self._conn.execute(
            "SELECT account_pattern FROM email_account_owners WHERE telegram_id = ?", (telegram_id,)
        )
        rows = await cur.fetchall()
        return [row["account_pattern"] for row in rows]

    # ── Pending recurring (one-tap confirm for recurring expenses) ────────────

    @_serialize_write
    async def set_pending_recurring(
        self, user_id: int, entry: ExpenseEntry, recurring_page_url: str | None, uid: str, sender: str
    ) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_recurring (user_id, entry_json, recurring_page_url, uid, sender, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, entry.model_dump_json(), recurring_page_url or "", uid, sender, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def get_pending_recurring(self, user_id: int) -> dict | None:
        cur = await self._conn.execute(
            "SELECT * FROM pending_recurring WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return {
            "entry": ExpenseEntry.model_validate_json(row["entry_json"]),
            "recurring_page_url": row["recurring_page_url"] or None,
            "uid": row["uid"],
            "sender": row["sender"],
        }

    @_serialize_write
    async def clear_pending_recurring(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_recurring WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Users (multi-tenant) ──────────────────────────────────────────────────

    async def get_user(self, telegram_id: int) -> UserRecord | None:
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    @_serialize_write
    async def upsert_user(self, telegram_id: int, **fields) -> None:
        allowed = {
            "owner_name", "notion_token", "setup_step",
            "expenses_ds", "subcategories_ds", "accounts_ds",
            "months_ds", "years_ds", "recurring_ds", "assets_ds",
            "income_ds", "income_subcategories_ds",
            "income_months_ds", "income_years_ds",
            "budget_ds", "categories_ds",
        }
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"Unexpected user columns: {bad}")
        # Encrypt notion_token before storage
        if "notion_token" in fields:
            fields["notion_token"] = _encrypt_token(fields["notion_token"], self._fernet)
        now = datetime.now(timezone.utc).isoformat()
        # Always include NOT NULL columns in the INSERT to avoid constraint violations
        # when only a subset of fields is provided (e.g. set_user_setup_step).
        required_cols = ["owner_name", "notion_token"]
        all_cols = list(fields.keys())
        insert_cols = ["telegram_id", "created_at", "updated_at"]
        insert_vals = [telegram_id, now, now]
        for col in all_cols:
            insert_cols.append(col)
            insert_vals.append(fields[col])
        for col in required_cols:
            if col not in all_cols:
                insert_cols.append(col)
                insert_vals.append(fields.get(col, ""))
        placeholders = ", ".join(["?"] * len(insert_cols))
        col_names = ", ".join(insert_cols)
        set_parts = ["updated_at = excluded.updated_at"] + [f"{k} = excluded.{k}" for k in all_cols]
        set_clause = ", ".join(set_parts)
        await self._conn.execute(
            f"INSERT INTO users ({col_names}) VALUES ({placeholders}) "
            f"ON CONFLICT(telegram_id) DO UPDATE SET {set_clause}",
            insert_vals,
        )
        await self._conn.commit()

    async def set_user_setup_step(self, telegram_id: int, step: str) -> None:
        await self.upsert_user(telegram_id, setup_step=step)

    async def get_user_by_name(self, owner_name: str) -> UserRecord | None:
        cur = await self._conn.execute(
            "SELECT * FROM users WHERE owner_name = ?", (owner_name,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def get_all_users(self) -> dict[int, UserRecord]:
        cur = await self._conn.execute("SELECT * FROM users")
        rows = await cur.fetchall()
        result: dict[int, UserRecord] = {}
        for row in rows:
            result[row["telegram_id"]] = self._row_to_user(row)
        return result
