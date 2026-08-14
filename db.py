import asyncio
import hashlib
from functools import wraps
import json
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken

from models import ExpenseEntry, EmailTransaction, IncomeEntry, UserRecord

log = logging.getLogger(__name__)


class TransactionConflictError(ValueError):
    """Raised when a client edits a transaction from an obsolete revision."""


class TransactionPreconditionRequiredError(ValueError):
    """Raised when an API mutation omits the required server revision."""


class SelfTransferMutationError(ValueError):
    """Raised when a self-transfer principal is mutated outside its bundle."""


class SelfTransferMatchAmbiguousError(ValueError):
    """Raised when an unreferenced capture has multiple canonical matches."""


@dataclass(frozen=True)
class AndroidSelfTransferOutcome:
    transaction: dict[str, Any]
    created: bool
    code: str
    action: str


_ACCOUNT_ALIASES = {
    "mandiri": "mandiri 1854",
    "mandiri 1854": "mandiri 1854",
    "bsi": "bsi 9400",
    "bsi 9400": "bsi 9400",
    "jago": "jago",
    "cash": "cash",
}


def _account_identity(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    return _ACCOUNT_ALIASES.get(normalized, normalized)


def _transfer_evidence_key(scheme: str, reference: str) -> tuple[str, str]:
    """Return a non-reversible key for an explicitly labelled bank reference."""
    if not isinstance(scheme, str) or scheme.strip().lower() != "bank_reference":
        raise ValueError("Unsupported self-transfer evidence scheme")
    if not isinstance(reference, str):
        raise ValueError("Self-transfer reference must be a string")
    normalized = re.sub(r"[^A-Z0-9]", "", reference.upper())
    if not 6 <= len(normalized) <= 96:
        raise ValueError("Self-transfer reference must contain 6-96 letters or digits")
    digest = hashlib.sha256(
        f"self-transfer-v1\0bank_reference\0{normalized}".encode()
    ).hexdigest()
    return "bank_reference", f"self-transfer-v1:{digest}"


def _transaction_evidence_key(scheme: str, reference: str) -> tuple[str, str]:
    """Return a non-reversible key for a canonical transaction reference.

    References are identity evidence, not display data.  Keeping only a domain
    separated digest lets independent captures be correlated without storing a
    bank's raw reference in the local ledger or API response.
    """
    if not isinstance(scheme, str) or scheme.strip().lower() != "bank_reference":
        raise ValueError("Unsupported transaction evidence scheme")
    if not isinstance(reference, str):
        raise ValueError("Bank reference must be a string")
    normalized = re.sub(r"[^A-Z0-9]", "", reference.upper())
    if not 6 <= len(normalized) <= 96:
        raise ValueError("Bank reference must contain 6-96 letters or digits")
    digest = hashlib.sha256(
        f"transaction-evidence-v1\0bank_reference\0{normalized}".encode()
    ).hexdigest()
    return "bank_reference", f"transaction-evidence-v1:{digest}"


def _same_transaction_identity(
    row: Any,
    *,
    kind: str,
    amount_idr: int,
    occurred_on: str,
    account: str,
) -> bool:
    """Allow cross-source merge only when immutable financial fields agree."""
    return (
        row["status"] != "voided"
        and row["kind"] == kind
        and row["amount_idr"] == amount_idr
        and row["occurred_on"] == occurred_on
        and _account_identity(row["account"]) == _account_identity(account)
    )


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

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 9")
        if await cur.fetchone() is None:
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS self_transfer_correlations (
                    user_id INTEGER NOT NULL,
                    evidence_key TEXT NOT NULL,
                    evidence_scheme TEXT NOT NULL,
                    capture_transaction_id TEXT REFERENCES transactions(id),
                    canonical_transaction_id TEXT REFERENCES transactions(id),
                    conflict_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, evidence_key)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_self_transfer_capture
                    ON self_transfer_correlations(capture_transaction_id)
                    WHERE capture_transaction_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_self_transfer_canonical
                    ON self_transfer_correlations(canonical_transaction_id)
                    WHERE canonical_transaction_id IS NOT NULL;
                """
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (9, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 10")
        if await cur.fetchone() is None:
            # Earlier self-transfer releases used expense/income for principal
            # legs without a role marker. Reclassify them as non-spending
            # principal rows and park unsupported Notion jobs.
            await self._conn.execute(
                "ALTER TABLE transactions ADD COLUMN ledger_role TEXT NOT NULL DEFAULT 'ordinary'"
            )
            await self._conn.execute(
                "ALTER TABLE transactions ADD COLUMN transfer_bundle_id TEXT"
            )
            await self._conn.execute(
                "ALTER TABLE transactions ADD COLUMN transfer_leg TEXT"
            )
            await self._conn.execute(
                "UPDATE transactions SET ledger_role='self_transfer_principal',"
                "transfer_leg=CASE WHEN description LIKE '%(keluar)' THEN 'outgoing' "
                "WHEN description LIKE '%(masuk)' THEN 'incoming' ELSE transfer_leg END "
                "WHERE description LIKE 'Transfer antar rekening — %'"
            )
            await self._conn.execute(
                "DELETE FROM sync_outbox WHERE completed_at IS NULL "
                "AND transaction_id IN (SELECT id FROM transactions WHERE ledger_role='self_transfer_principal')"
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (10, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 11")
        if await cur.fetchone() is None:
            await self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_assets (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('asset', 'liability')),
                    asset_type TEXT NOT NULL DEFAULT '',
                    value_idr INTEGER CHECK (value_idr IS NULL OR value_idr >= 0),
                    notes TEXT NOT NULL DEFAULT '',
                    as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_local_assets_user_kind
                    ON local_assets(user_id, kind, updated_at DESC);

                CREATE TABLE IF NOT EXISTS transaction_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    evidence_scheme TEXT,
                    evidence_key TEXT,
                    captured_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(user_id, source, source_ref)
                );
                CREATE INDEX IF NOT EXISTS idx_transaction_evidence_reference
                    ON transaction_evidence(user_id, evidence_key)
                    WHERE evidence_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_transaction_evidence_transaction
                    ON transaction_evidence(transaction_id, captured_at);
                """
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (11, ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            await self._conn.commit()

        cur = await self._conn.execute("SELECT 1 FROM schema_migrations WHERE version = 12")
        if await cur.fetchone() is None:
            columns = await self._conn.execute("PRAGMA table_info(local_assets)")
            names = {row["name"] for row in await columns.fetchall()}
            additions = {
                "quantity": "REAL",
                "unit": "TEXT NOT NULL DEFAULT ''",
                "last_updated": "TEXT",
            }
            for name, definition in additions.items():
                if name not in names:
                    await self._conn.execute(
                        f"ALTER TABLE local_assets ADD COLUMN {name} {definition}"
                    )
            await self._conn.execute(
                "UPDATE local_assets SET last_updated=as_of WHERE last_updated IS NULL"
            )
            await self._conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (12, ?)",
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
        """Allow retry only when the UID was not deterministically rejected.

        Rejected messages intentionally exist in both processed_emails (to
        exclude them from polling) and email_processing_failures (for audit).
        Clearing only their failure row would claim to enable a retry while
        leaving the UID excluded and erasing the rejection evidence.
        """
        cur = await self._conn.execute(
            "DELETE FROM email_processing_failures WHERE uid=? "
            "AND NOT EXISTS (SELECT 1 FROM processed_emails WHERE uid=?)",
            (uid, uid),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    @_serialize_write
    async def dismiss_email_processing_failure(self, uid: str) -> bool:
        """Acknowledge a terminal email failure without retrying its UID."""
        uid = str(uid).strip()
        if not uid:
            return False
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = await (
                await self._conn.execute(
                    "SELECT sender FROM email_processing_failures WHERE uid=?", (uid,)
                )
            ).fetchone()
            if row is None:
                await self._conn.rollback()
                return False
            await self._conn.execute(
                "INSERT OR REPLACE INTO processed_emails(uid,sender,processed_at) VALUES (?,?,?)",
                (uid, row["sender"], now),
            )
            await self._conn.execute(
                "DELETE FROM email_processing_failures WHERE uid=?", (uid,)
            )
            await self._conn.commit()
            return True
        except Exception:
            await self._conn.rollback()
            raise

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
            "t.notion_page_id,t.recurring_page_id,t.ledger_role "
            "FROM sync_outbox o JOIN transactions t ON t.id=o.transaction_id "
            "WHERE o.completed_at IS NULL "
            "AND t.ledger_role != 'self_transfer_principal' "
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
            "WHERE t.user_id=? AND o.completed_at IS NULL "
            "AND t.ledger_role != 'self_transfer_principal'",
            (user_id,),
        )
        summary = await cur.fetchone()
        errors = await self._conn.execute(
            "SELECT o.id AS outbox_id,t.id AS transaction_id,o.attempt_count,"
            "o.last_error,o.next_attempt_at "
            "FROM sync_outbox o JOIN transactions t ON t.id=o.transaction_id "
            "WHERE t.user_id=? AND o.completed_at IS NULL "
            "AND t.ledger_role != 'self_transfer_principal' "
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
            "AND transaction_id IN (SELECT id FROM transactions "
            "WHERE user_id=? AND ledger_role != 'self_transfer_principal')",
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
        where = "t.user_id = ?"
        if status is not None:
            if status not in {"pending", "confirmed", "voided"}:
                raise ValueError("Invalid transaction status")
            where += " AND t.status = ?"
            params.append(status)
        params.append(limit)
        cur = await self._conn.execute(
            f"SELECT t.*, (SELECT COUNT(*) FROM transaction_evidence e "
            f"WHERE e.transaction_id=t.id) AS evidence_count FROM transactions t WHERE {where} "
            "ORDER BY t.occurred_on DESC, t.created_at DESC LIMIT ?",
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
        where = "t.user_id=? AND t.status IN ('confirmed','voided')"
        if after_updated_at is not None or after_id is not None:
            if not after_updated_at or not after_id:
                raise ValueError("cursor is incomplete")
            where += " AND (t.updated_at > ? OR (t.updated_at = ? AND t.id > ?))"
            params.extend([after_updated_at, after_updated_at, after_id])
        params.append(limit + 1)
        # The change feed and its cursor must never observe this shared
        # connection between a writer's BEGIN and commit/rollback. SQLite
        # connections see their own uncommitted writes, so an unlocked read
        # could otherwise publish a revision that is subsequently rolled back.
        async with self._write_lock:
            cur = await self._conn.execute(
                f"SELECT t.*, (SELECT COUNT(*) FROM transaction_evidence e "
                f"WHERE e.transaction_id=t.id) AS evidence_count FROM transactions t WHERE {where} "
                "ORDER BY t.updated_at ASC, t.id ASC LIMIT ?",
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
            "SELECT t.*, (SELECT COUNT(*) FROM transaction_evidence e "
            "WHERE e.transaction_id=t.id) AS evidence_count "
            "FROM transactions t WHERE user_id=? AND id=? LIMIT 1",
            (user_id, transaction_id),
        )
        row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def get_transaction_evidence(
        self, user_id: int, transaction_id: str
    ) -> list[dict[str, Any]]:
        """Return a deliberately non-sensitive representation of source proof."""
        cur = await self._conn.execute(
            "SELECT source,evidence_scheme,captured_at FROM transaction_evidence "
            "WHERE user_id=? AND transaction_id=? ORDER BY captured_at ASC,id ASC",
            (user_id, transaction_id),
        )
        return [
            {
                "source": row["source"],
                "has_bank_reference": row["evidence_scheme"] == "bank_reference",
                "captured_at": row["captured_at"],
            }
            for row in await cur.fetchall()
        ]

    async def _attach_transaction_evidence(
        self,
        *,
        user_id: int,
        transaction_id: str,
        source: str,
        source_ref: str,
        evidence_scheme: str | None,
        evidence_key: str | None,
        metadata: dict[str, Any] | None,
        captured_at: str,
    ) -> None:
        """Attach one source record while the caller owns a write transaction."""
        await self._conn.execute(
            "INSERT INTO transaction_evidence "
            "(user_id,transaction_id,source,source_ref,evidence_scheme,evidence_key,"
            "captured_at,metadata_json) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,source,source_ref) DO NOTHING",
            (
                user_id, transaction_id, source, source_ref, evidence_scheme,
                evidence_key, captured_at, json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )

    async def list_local_assets(self, user_id: int) -> list[dict[str, Any]]:
        async with self._write_lock:
            cur = await self._conn.execute(
                "SELECT * FROM local_assets WHERE user_id=? ORDER BY kind,name COLLATE NOCASE,id",
                (user_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    async def list_confirmed_self_transfer_legs(self, user_id: int) -> list[dict[str, Any]]:
        """Return confirmed principal legs used to adjust Notion balance snapshots.

        Principal legs intentionally never enter the Notion outbox, so the
        portfolio reader needs their local directional effect when showing
        account balances.
        """
        async with self._write_lock:
            cur = await self._conn.execute(
                "SELECT kind,amount_idr,account FROM transactions "
                "WHERE user_id=? AND status='confirmed' "
                "AND ledger_role='self_transfer_principal'",
                (user_id,),
            )
            return [dict(row) for row in await cur.fetchall()]

    @_serialize_write
    async def create_local_asset(
        self, user_id: int, *, name: str, kind: str, asset_type: str = "",
        value_idr: int | None = None, quantity: float | int | None = None,
        unit: str = "", notes: str = "", last_updated: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not (name := name.strip()):
            raise ValueError("name is required")
        if kind not in {"asset", "liability"}:
            raise ValueError("kind must be asset or liability")
        if value_idr is not None and (isinstance(value_idr, bool) or not isinstance(value_idr, int) or value_idr < 0):
            raise ValueError("value_idr must be a nonnegative integer or null")
        if quantity is not None and (isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity < 0):
            raise ValueError("quantity must be a nonnegative finite number or null")
        if not isinstance(unit, str):
            raise ValueError("unit must be a string")
        last_updated = last_updated if last_updated is not None else as_of
        if last_updated is not None:
            last_updated = _canonical_occurred_on(last_updated)
        now = datetime.now(timezone.utc).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()
        asset_id = str(uuid.uuid4())
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            await self._conn.execute(
                "INSERT INTO local_assets(id,user_id,name,kind,asset_type,value_idr,quantity,unit,notes,as_of,last_updated,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (asset_id, user_id, name, kind, str(asset_type).strip(), value_idr,
                 quantity, unit.strip(), str(notes).strip(), last_updated or today,
                 last_updated or today, now, now),
            )
            await self._conn.commit()
            row = await (await self._conn.execute(
                "SELECT * FROM local_assets WHERE id=?", (asset_id,)
            )).fetchone()
            return dict(row)
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def update_local_asset(
        self, user_id: int, asset_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"name", "kind", "asset_type", "value_idr", "quantity", "unit", "notes", "last_updated", "as_of"}
        if not changes or set(changes) - allowed:
            raise ValueError("No valid asset fields supplied")
        if "name" in changes and (not isinstance(changes["name"], str) or not changes["name"].strip()):
            raise ValueError("name is required")
        if "kind" in changes and changes["kind"] not in {"asset", "liability"}:
            raise ValueError("kind must be asset or liability")
        if "value_idr" in changes and changes["value_idr"] is not None and (
            isinstance(changes["value_idr"], bool) or not isinstance(changes["value_idr"], int) or changes["value_idr"] < 0
        ):
            raise ValueError("value_idr must be a nonnegative integer or null")
        if "quantity" in changes and changes["quantity"] is not None and (
            isinstance(changes["quantity"], bool) or not isinstance(changes["quantity"], (int, float))
            or not math.isfinite(changes["quantity"]) or changes["quantity"] < 0
        ):
            raise ValueError("quantity must be a nonnegative finite number or null")
        if "as_of" in changes and "last_updated" in changes:
            raise ValueError("Use last_updated instead of as_of")
        if "as_of" in changes:
            changes["last_updated"] = _canonical_occurred_on(changes.pop("as_of"))
        if "last_updated" in changes:
            changes["last_updated"] = _canonical_occurred_on(changes["last_updated"])
        for field in {"name", "asset_type", "unit", "notes"} & changes.keys():
            if not isinstance(changes[field], str):
                raise ValueError(f"{field} must be a string")
            changes[field] = changes[field].strip()
        now = datetime.now(timezone.utc).isoformat()
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = await (await self._conn.execute(
                "SELECT * FROM local_assets WHERE id=? AND user_id=?", (asset_id, user_id)
            )).fetchone()
            if current is None:
                await self._conn.rollback()
                return None
            assignments = ", ".join(f"{field}=?" for field in changes) + ", updated_at=?"
            await self._conn.execute(
                f"UPDATE local_assets SET {assignments} WHERE id=? AND user_id=?",
                [*changes.values(), now, asset_id, user_id],
            )
            await self._conn.commit()
            row = await (await self._conn.execute(
                "SELECT * FROM local_assets WHERE id=?", (asset_id,)
            )).fetchone()
            return dict(row)
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def delete_local_asset(self, user_id: int, asset_id: str) -> bool:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = await self._conn.execute(
                "DELETE FROM local_assets WHERE id=? AND user_id=?", (asset_id, user_id)
            )
            await self._conn.commit()
            return cur.rowcount == 1
        except Exception:
            await self._conn.rollback()
            raise

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
        bank_reference: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create an idempotent pending ingested ledger row.

        Returns ``(row, created)``. Replaying the same source reference for the
        same user returns the existing row without appending another event.

        ``android_notification`` is the default ingestion source. ``manual``
        is reserved for intentional entries created by the Android UI; it uses
        the same source-ref idempotency boundary and is never suppressed at
        creation time. Cross-source rows are not merged because this payload
        only an explicit bank reference can merge cross-source evidence.
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
        evidence_scheme = evidence_key = None
        if bank_reference is not None:
            evidence_scheme, evidence_key = _transaction_evidence_key(
                "bank_reference", bank_reference
            )
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
                if existing["ledger_role"] == "self_transfer_principal":
                    # A user can correct a captured transfer back to an
                    # ordinary expense/income using the same source reference.
                    # Do not replay the stale principal row: reclassify it
                    # atomically and restore normal Notion outbox behavior.
                    await self._conn.execute(
                        "UPDATE transactions SET kind=?,ledger_role='ordinary',"
                        "transfer_bundle_id=NULL,transfer_leg=NULL,amount_idr=?,"
                        "occurred_on=?,description=?,merchant=?,category=?,"
                        "subcategory=?,account=?,updated_at=? WHERE id=?",
                        (
                            kind, amount_idr, occurred_on, description.strip(),
                            merchant.strip(), category.strip(), subcategory.strip(),
                            account.strip(), now, existing["id"],
                        ),
                    )
                    await self._conn.execute(
                        "DELETE FROM self_transfer_correlations WHERE capture_transaction_id=?",
                        (existing["id"],),
                    )
                    await self._conn.execute(
                        "INSERT INTO transaction_events "
                        "(transaction_id,event_type,metadata_json,created_at) "
                        "VALUES (?,'reclassified_from_self_transfer',?,?)",
                        (
                            existing["id"],
                            json.dumps({"new_ledger_role": "ordinary"}, ensure_ascii=False),
                            now,
                        ),
                    )
                    if existing["status"] == "confirmed":
                        await self._conn.execute(
                            "INSERT OR IGNORE INTO sync_outbox "
                            "(transaction_id,operation,created_at,updated_at) VALUES (?,'upsert',?,?)",
                            (existing["id"], now, now),
                        )
                    await self._conn.commit()
                    refreshed = await (
                        await self._conn.execute(
                            "SELECT * FROM transactions WHERE id=?", (existing["id"],)
                        )
                    ).fetchone()
                    return dict(refreshed), False
                await self._conn.rollback()
                return dict(existing), False

            evidence_conflict_with: str | None = None
            if evidence_key is not None:
                candidates = await (
                    await self._conn.execute(
                        "SELECT t.* FROM transaction_evidence e JOIN transactions t "
                        "ON t.id=e.transaction_id WHERE e.user_id=? AND e.evidence_key=? "
                        "ORDER BY e.id ASC",
                        (user_id, evidence_key),
                    )
                ).fetchall()
                canonical = next(
                    (
                        candidate for candidate in candidates
                        if _same_transaction_identity(
                            candidate, kind=kind, amount_idr=amount_idr,
                            occurred_on=occurred_on, account=account,
                        )
                    ),
                    None,
                )
                if canonical is not None:
                    await self._attach_transaction_evidence(
                        user_id=user_id, transaction_id=canonical["id"], source=source,
                        source_ref=source_ref, evidence_scheme=evidence_scheme,
                        evidence_key=evidence_key, metadata=metadata, captured_at=now,
                    )
                    await self._conn.commit()
                    return dict(canonical), False
                if candidates:
                    raise TransactionConflictError(
                        "Bank reference already belongs to a transaction with different financial fields"
                    )

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
            event_metadata = dict(metadata or {})
            if evidence_conflict_with:
                event_metadata["evidence_conflict_with"] = evidence_conflict_with
            await self._conn.execute(
                "INSERT INTO transaction_events "
                "(transaction_id,event_type,metadata_json,created_at) VALUES (?, 'ingested', ?, ?)",
                (tx_id, json.dumps(event_metadata, ensure_ascii=False), now),
            )
            if evidence_conflict_with:
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) VALUES (?, 'evidence_conflict', ?, ?)",
                    (evidence_conflict_with, json.dumps({"conflict_transaction_id": tx_id}, ensure_ascii=False), now),
                )
            await self._attach_transaction_evidence(
                user_id=user_id, transaction_id=tx_id, source=source,
                source_ref=source_ref, evidence_scheme=evidence_scheme,
                evidence_key=evidence_key, metadata=metadata, captured_at=now,
            )
            await self._conn.commit()
            cur = await self._conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
            return dict(await cur.fetchone()), True
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def ingest_android_self_transfer(
        self,
        user_id: int,
        *,
        kind: str = "income",
        amount_idr: int,
        occurred_on: str,
        description: str,
        source_ref: str,
        evidence_scheme: str | None = None,
        evidence_reference: str | None = None,
        merchant: str = "",
        category: str = "",
        subcategory: str = "",
        account: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> AndroidSelfTransferOutcome:
        """Stage one transfer leg until the matching bank email proves it.

        Transfer principal keeps its directional ``kind`` for compatibility;
        ``ledger_role`` marks it as a non-spending principal leg.
        """
        if kind not in {"expense", "income"}:
            raise ValueError("Self-transfer kind must be expense or income")
        if isinstance(amount_idr, bool) or not isinstance(amount_idr, int) or amount_idr <= 0:
            raise ValueError("amount_idr must be a positive integer")
        occurred_on = _canonical_occurred_on(occurred_on)
        source_ref = source_ref.strip()
        if not source_ref:
            raise ValueError("source_ref is required")
        if (evidence_scheme is None) != (evidence_reference is None):
            raise ValueError("Self-transfer evidence is incomplete")
        evidence_key = None
        transaction_evidence_scheme = transaction_evidence_key = None
        if evidence_scheme is not None and evidence_reference is not None:
            evidence_scheme, evidence_key = _transfer_evidence_key(
                evidence_scheme, evidence_reference
            )
            transaction_evidence_scheme, transaction_evidence_key = _transaction_evidence_key(
                evidence_scheme, evidence_reference
            )
        now = datetime.now(timezone.utc).isoformat()

        def compatible(row: Any) -> bool:
            return (
                row["kind"] == kind
                and row["ledger_role"] == "self_transfer_principal"
                and row["amount_idr"] == amount_idr
                and _account_identity(row["account"]) == _account_identity(account)
            )

        async def canonical_matches() -> list[Any]:
            """Self-transfer-only fallback for banks that omit a reference.

            This intentionally requires one exact directional ledger leg. It is
            not used for ordinary expenses/income, and ambiguous candidates are
            left for review rather than selected heuristically.
            """
            matches = await (
                await self._conn.execute(
                    "SELECT * FROM transactions WHERE user_id=? AND kind=? "
                    "AND ledger_role='self_transfer_principal' AND status='confirmed' "
                    "AND NOT EXISTS (SELECT 1 FROM transaction_evidence e "
                    "WHERE e.transaction_id=transactions.id AND e.source='android_notification') "
                    "AND amount_idr=? AND occurred_on=? "
                    "ORDER BY created_at ASC,id ASC",
                    (user_id, kind, amount_idr, occurred_on),
                )
            ).fetchall()
            return [
                row for row in matches
                if _account_identity(row["account"]) == _account_identity(account)
            ]

        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            correlation = await (
                await self._conn.execute(
                    "SELECT * FROM self_transfer_correlations "
                    "WHERE user_id=? AND evidence_key=?",
                    (user_id, evidence_key),
                )
            ).fetchone()
            if correlation is not None:
                transaction_id = (
                    correlation["canonical_transaction_id"]
                    or correlation["capture_transaction_id"]
                )
                row = await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=? AND user_id=?",
                        (transaction_id, user_id),
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeError("Self-transfer correlation points to a missing transaction")
                if correlation["conflict_reason"] or not compatible(row):
                    if not correlation["conflict_reason"]:
                        await self._conn.execute(
                            "UPDATE self_transfer_correlations SET conflict_reason=?,updated_at=? "
                            "WHERE user_id=? AND evidence_key=?",
                            ("evidence_payload_conflict", now, user_id, evidence_key),
                        )
                        await self._conn.commit()
                    else:
                        await self._conn.rollback()
                    return AndroidSelfTransferOutcome(
                        dict(row), False, "evidence_conflict", "keep_review"
                    )
                await self._conn.rollback()
                if correlation["canonical_transaction_id"]:
                    return AndroidSelfTransferOutcome(
                        dict(row), False, "reused_canonical_transfer", "finalize"
                    )
                return AndroidSelfTransferOutcome(
                    dict(row), False, "awaiting_canonical_email", "keep_review"
                )

            existing = await (
                await self._conn.execute(
                    "SELECT * FROM transactions WHERE user_id=? "
                    "AND source='android_notification' AND source_ref=?",
                    (user_id, source_ref),
                )
            ).fetchone()
            if existing is not None:
                if existing["status"] == "confirmed":
                    await self._conn.rollback()
                    return AndroidSelfTransferOutcome(
                        dict(existing), False, "reused_canonical_transfer", "finalize"
                    )
                await self._conn.rollback()
                return AndroidSelfTransferOutcome(
                    dict(existing), False, "awaiting_canonical_email", "keep_review"
                )

            ambiguous_candidates = False
            if evidence_key is None:
                matches = await canonical_matches()
                if len(matches) == 1:
                    canonical = matches[0]
                    await self._attach_transaction_evidence(
                        user_id=user_id, transaction_id=canonical["id"],
                        source="android_notification", source_ref=source_ref,
                        evidence_scheme=None, evidence_key=None, metadata=metadata,
                        captured_at=now,
                    )
                    await self._conn.commit()
                    return AndroidSelfTransferOutcome(
                        dict(canonical), False, "reused_canonical_transfer", "finalize"
                    )
                ambiguous_candidates = len(matches) > 1

            tx_id = str(uuid.uuid4())
            await self._conn.execute(
                "INSERT INTO transactions "
                "(id,user_id,kind,ledger_role,status,amount_idr,occurred_on,description,merchant,"
                "category,subcategory,account,source,source_ref,created_at,updated_at) "
                "VALUES (?,?,?,'self_transfer_principal','pending',?,?,?,?,?,?,?,'android_notification',?,?,?)",
                (
                    tx_id, user_id, kind, amount_idr, occurred_on, description.strip(),
                    merchant.strip(), category.strip(), subcategory.strip(), account.strip(),
                    source_ref, now, now,
                ),
            )
            event_metadata = dict(metadata or {})
            event_metadata.update(
                {
                    "self_transfer": True,
                    "capture_kind": kind,
                    "transfer_evidence_key": evidence_key,
                    "ambiguous_candidates": ambiguous_candidates,
                }
            )
            await self._conn.execute(
                "INSERT INTO transaction_events "
                "(transaction_id,event_type,metadata_json,created_at) "
                "VALUES (?,'self_transfer_capture',?,?)",
                (tx_id, json.dumps(event_metadata, ensure_ascii=False), now),
            )
            await self._attach_transaction_evidence(
                user_id=user_id, transaction_id=tx_id, source="android_notification",
                source_ref=source_ref, evidence_scheme=transaction_evidence_scheme,
                evidence_key=transaction_evidence_key, metadata=metadata, captured_at=now,
            )
            if evidence_key:
                await self._conn.execute(
                    "INSERT INTO self_transfer_correlations "
                    "(user_id,evidence_key,evidence_scheme,capture_transaction_id,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (user_id, evidence_key, evidence_scheme, tx_id, now, now),
                )
            await self._conn.commit()
            row = await (
                await self._conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
            ).fetchone()
            return AndroidSelfTransferOutcome(
                dict(row), True,
                "ambiguous_candidates" if ambiguous_candidates else "awaiting_canonical_email",
                "keep_review",
            )
        except Exception:
            await self._conn.rollback()
            raise

    @_serialize_write
    async def create_confirmed_self_transfer(
        self,
        user_id: int,
        *,
        amount_idr: int,
        admin_fee_idr: int,
        occurred_on: str,
        outgoing_description: str,
        incoming_description: str,
        fee_description: str,
        outgoing_subcategory: str,
        incoming_subcategory: str,
        source_account: str,
        destination_account: str,
        email_uid: str,
        sender: str,
        evidence_scheme: str | None = None,
        evidence_reference: str | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        """Atomically create a self-transfer bundle and correlate its incoming leg."""
        if isinstance(amount_idr, bool) or not isinstance(amount_idr, int) or amount_idr <= 0:
            raise ValueError("amount_idr must be a positive integer")
        if isinstance(admin_fee_idr, bool) or not isinstance(admin_fee_idr, int) or admin_fee_idr < 0:
            raise ValueError("admin_fee_idr must be a nonnegative integer")
        occurred_on = _canonical_occurred_on(occurred_on)
        email_uid = email_uid.strip()
        if not email_uid:
            raise ValueError("email_uid is required")
        evidence_key = None
        transaction_evidence_scheme = transaction_evidence_key = None
        if evidence_scheme is not None or evidence_reference is not None:
            if evidence_scheme is None or evidence_reference is None:
                raise ValueError("Self-transfer evidence is incomplete")
            evidence_scheme, evidence_key = _transfer_evidence_key(
                evidence_scheme, evidence_reference
            )
            transaction_evidence_scheme, transaction_evidence_key = _transaction_evidence_key(
                evidence_scheme, evidence_reference
            )
        now = datetime.now(timezone.utc).isoformat()
        transfer_bundle_id = "self-transfer-" + hashlib.sha256(
            f"{user_id}:{email_uid}".encode()
        ).hexdigest()[:32]

        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            async def existing_source(source_ref: str) -> Any:
                return await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE user_id=? "
                        "AND source='bank_email' AND source_ref=?",
                        (user_id, source_ref),
                    )
                ).fetchone()

            async def insert_confirmed(
                *, kind: str, amount: int, description: str,
                subcategory: str, account: str, source_ref: str, component: str,
                ledger_role: str = "ordinary", transfer_bundle_id: str | None = None,
                transfer_leg: str | None = None,
            ) -> Any:
                existing = await existing_source(source_ref)
                if existing is not None:
                    return existing
                tx_id = str(uuid.uuid4())
                await self._conn.execute(
                    "INSERT INTO transactions "
                    "(id,user_id,kind,ledger_role,transfer_bundle_id,transfer_leg,status,"
                    "amount_idr,occurred_on,description,merchant,"
                    "category,subcategory,account,source,source_ref,created_at,updated_at,confirmed_at) "
                    "VALUES (?,?,?,?,?,?,'confirmed',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        tx_id, user_id, kind, ledger_role, transfer_bundle_id, transfer_leg,
                        amount, occurred_on, description, "", "", subcategory, account,
                        "bank_email", source_ref, now, now, now,
                    ),
                )
                event_metadata = {
                    "email_uid": email_uid,
                    "sender": sender,
                    "component": component,
                }
                if transfer_bundle_id:
                    event_metadata["transfer_bundle_id"] = transfer_bundle_id
                if evidence_key:
                    event_metadata["transfer_evidence_key"] = evidence_key
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) "
                    "VALUES (?,'confirmed_external',?,?)",
                    (tx_id, json.dumps(event_metadata, ensure_ascii=False), now),
                )
                if component == "transfer-in":
                    await self._attach_transaction_evidence(
                        user_id=user_id, transaction_id=tx_id, source="bank_email",
                        source_ref=source_ref, evidence_scheme=transaction_evidence_scheme,
                        evidence_key=transaction_evidence_key,
                        metadata={"email_uid": email_uid, "sender": sender}, captured_at=now,
                    )
                if ledger_role != "self_transfer_principal":
                    await self._conn.execute(
                        "INSERT INTO sync_outbox "
                        "(transaction_id,operation,created_at,updated_at) "
                        "VALUES (?,'upsert',?,?)",
                        (tx_id, now, now),
                    )
                return await (
                    await self._conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))
                ).fetchone()

            correlation = None
            if evidence_key:
                correlation = await (
                    await self._conn.execute(
                        "SELECT * FROM self_transfer_correlations "
                        "WHERE user_id=? AND evidence_key=?",
                        (user_id, evidence_key),
                    )
                ).fetchone()

            async def capture_direction(capture: Any) -> str | None:
                event = await (
                    await self._conn.execute(
                        "SELECT metadata_json FROM transaction_events "
                        "WHERE transaction_id=? AND event_type='self_transfer_capture' "
                        "ORDER BY created_at DESC, id DESC LIMIT 1",
                        (capture["id"],),
                    )
                ).fetchone()
                metadata = json.loads(event["metadata_json"] or "{}") if event else {}
                return metadata.get("capture_kind") or {
                    "income": "income",
                    "expense": "expense",
                }.get(capture["kind"])

            async def pending_capture(capture_kind: str, account_name: str) -> list[Any]:
                rows = await (
                    await self._conn.execute(
                        "SELECT t.* FROM transactions t WHERE t.user_id=? "
                        "AND t.source='android_notification' AND t.status='pending' "
                        "AND t.kind=? AND t.ledger_role='self_transfer_principal' "
                        "AND t.amount_idr=? AND t.occurred_on=? "
                        "ORDER BY t.created_at ASC,t.id ASC",
                        (user_id, capture_kind, amount_idr, occurred_on),
                    )
                ).fetchall()
                return [
                    row for row in rows
                    if _account_identity(row["account"]) == _account_identity(account_name)
                ]

            async def canonicalize_capture(
                capture: Any,
                *,
                description: str,
                subcategory: str,
                account: str,
                kind: str,
                leg: str,
                component: str,
            ) -> Any:
                await self._conn.execute(
                    "UPDATE transactions SET kind=?,ledger_role='self_transfer_principal',"
                    "transfer_bundle_id=?,transfer_leg=?,status='confirmed',amount_idr=?,occurred_on=?,"
                    "description=?,merchant='',category='',subcategory=?,account=?,"
                    "confirmed_at=COALESCE(confirmed_at,?),updated_at=? WHERE id=?",
                    (
                        kind, transfer_bundle_id, leg, amount_idr, occurred_on,
                        description, subcategory, account, now, now,
                        capture["id"],
                    ),
                )
                await self._attach_transaction_evidence(
                    user_id=user_id, transaction_id=capture["id"], source="bank_email",
                    source_ref=f"gmail:{email_uid}:{component}",
                    evidence_scheme=transaction_evidence_scheme,
                    evidence_key=transaction_evidence_key,
                    metadata={"email_uid": email_uid, "sender": sender}, captured_at=now,
                )
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) "
                    "VALUES (?,'self_transfer_canonicalized',?,?)",
                    (
                        capture["id"],
                        json.dumps(
                            {
                                "email_uid": email_uid,
                                "sender": sender,
                                "component": component,
                                "transfer_evidence_key": evidence_key,
                            },
                            ensure_ascii=False,
                        ),
                        now,
                    ),
                )
                # Transfer principal is authoritative in SQLite/API but has no
                # matching Notion database. Only the admin fee is synced there.
                return await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=?", (capture["id"],)
                    )
                ).fetchone()

            correlation_capture = None
            if correlation and correlation["capture_transaction_id"]:
                correlation_capture = await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=? AND user_id=?",
                        (correlation["capture_transaction_id"], user_id),
                    )
                ).fetchone()

            outgoing_capture = None
            incoming_capture = None
            conflict_reason = None
            if correlation_capture is not None:
                direction = await capture_direction(correlation_capture)
                compatible = (
                    correlation_capture["kind"] in {"expense", "income"}
                    and correlation_capture["ledger_role"] == "self_transfer_principal"
                    and correlation_capture["amount_idr"] == amount_idr
                )
                if compatible and direction == "expense" and _account_identity(correlation_capture["account"]) == _account_identity(source_account):
                    outgoing_capture = correlation_capture
                elif compatible and direction == "income" and _account_identity(correlation_capture["account"]) == _account_identity(destination_account):
                    incoming_capture = correlation_capture
                else:
                    conflict_reason = "evidence_payload_conflict"
            elif evidence_key is None:
                outgoing_candidates = await pending_capture("expense", source_account)
                incoming_candidates = await pending_capture("income", destination_account)
                if len(outgoing_candidates) == 1:
                    outgoing_capture = outgoing_candidates[0]
                elif len(outgoing_candidates) > 1:
                    conflict_reason = "ambiguous_android_outgoing_capture"
                if len(incoming_candidates) == 1:
                    incoming_capture = incoming_candidates[0]
                elif len(incoming_candidates) > 1:
                    conflict_reason = conflict_reason or "ambiguous_android_incoming_capture"
            if outgoing_capture is not None:
                outgoing = await canonicalize_capture(
                    outgoing_capture,
                    description=outgoing_description,
                    subcategory=outgoing_subcategory,
                    account=source_account,
                    kind="expense",
                    leg="outgoing",
                    component="transfer-out",
                )
            else:
                outgoing = await insert_confirmed(
                    kind="expense", amount=amount_idr,
                    description=outgoing_description, subcategory=outgoing_subcategory,
                    account=source_account, source_ref=f"gmail:{email_uid}:transfer-out",
                    ledger_role="self_transfer_principal",
                    transfer_bundle_id=transfer_bundle_id,
                    transfer_leg="outgoing",
                    component="transfer-out",
                )

            incoming = None
            if incoming_capture is not None:
                incoming = await canonicalize_capture(
                    incoming_capture,
                    description=incoming_description,
                    subcategory=incoming_subcategory,
                    account=destination_account,
                    kind="income",
                    leg="incoming",
                    component="transfer-in",
                )
            elif correlation and correlation["canonical_transaction_id"]:
                incoming = await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=? AND user_id=?",
                        (correlation["canonical_transaction_id"], user_id),
                    )
                ).fetchone()

            if incoming is None:
                incoming = await insert_confirmed(
                    kind="income", amount=amount_idr,
                    description=incoming_description,
                    subcategory=incoming_subcategory,
                    account=destination_account,
                    source_ref=f"gmail:{email_uid}:transfer-in",
                    ledger_role="self_transfer_principal",
                    transfer_bundle_id=transfer_bundle_id,
                    transfer_leg="incoming",
                    component="transfer-in",
                )

            if evidence_key:
                canonical_id = (
                    correlation["canonical_transaction_id"]
                    if correlation and correlation["canonical_transaction_id"]
                    else outgoing["id"] if outgoing_capture is not None
                    else incoming["id"] if incoming_capture is not None
                    else incoming["id"]
                )
                capture_id = (
                    correlation["capture_transaction_id"] if correlation else None
                )
                await self._conn.execute(
                    "INSERT INTO self_transfer_correlations "
                    "(user_id,evidence_key,evidence_scheme,capture_transaction_id,"
                    "canonical_transaction_id,conflict_reason,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,evidence_key) DO UPDATE SET "
                    "canonical_transaction_id=excluded.canonical_transaction_id,"
                    "conflict_reason=COALESCE(self_transfer_correlations.conflict_reason,excluded.conflict_reason),"
                    "updated_at=excluded.updated_at",
                    (
                        user_id, evidence_key, evidence_scheme, capture_id,
                        canonical_id, conflict_reason, now, now,
                    ),
                )

            if conflict_reason:
                # Never publish a bank email that contradicts an existing
                # capture or has ambiguous reference-less candidates. Keep
                # the new legs reviewable, but exclude them from balances and
                # Notion until the evidence is explicitly resolved.
                for leg in (outgoing, incoming):
                    if leg is not None and leg["source"] == "bank_email":
                        await self._conn.execute(
                            "UPDATE transactions SET status='pending',confirmed_at=NULL WHERE id=?",
                            (leg["id"],),
                        )
                        await self._conn.execute(
                            "DELETE FROM sync_outbox WHERE transaction_id=? AND completed_at IS NULL",
                            (leg["id"],),
                        )
                        await self._conn.execute(
                            "INSERT INTO transaction_events "
                            "(transaction_id,event_type,metadata_json,created_at) "
                            "VALUES (?,'evidence_conflict',?,?)",
                            (
                                leg["id"],
                                json.dumps({"reason": conflict_reason}, ensure_ascii=False),
                        now,
                    ),
                )
                outgoing = await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=?", (outgoing["id"],)
                    )
                ).fetchone()
                incoming = await (
                    await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=?", (incoming["id"],)
                    )
                ).fetchone()

            fee = None
            if admin_fee_idr:
                fee = await insert_confirmed(
                    kind="expense", amount=admin_fee_idr,
                    description=fee_description, subcategory=outgoing_subcategory,
                    account=source_account, source_ref=f"gmail:{email_uid}:fee",
                    ledger_role="self_transfer_fee",
                    transfer_bundle_id=transfer_bundle_id,
                    transfer_leg="fee",
                    component="fee",
                )
            await self._conn.commit()
            return {
                "outgoing": dict(outgoing),
                "incoming": dict(incoming),
                "fee": dict(fee) if fee is not None else None,
            }
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
        ledger_role: str = "ordinary",
        transfer_bundle_id: str | None = None,
        transfer_leg: str | None = None,
        bank_reference: str | None = None,
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
        evidence_scheme = evidence_key = None
        if bank_reference is not None:
            evidence_scheme, evidence_key = _transaction_evidence_key(
                "bank_reference", bank_reference
            )
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
            evidence_conflict_with: str | None = None
            if evidence_key is not None:
                candidates = await (
                    await self._conn.execute(
                        "SELECT t.* FROM transaction_evidence e JOIN transactions t "
                        "ON t.id=e.transaction_id WHERE e.user_id=? AND e.evidence_key=? "
                        "ORDER BY e.id ASC",
                        (user_id, evidence_key),
                    )
                ).fetchall()
                canonical = next(
                    (
                        candidate for candidate in candidates
                        if _same_transaction_identity(
                            candidate, kind=kind, amount_idr=amount_idr,
                            occurred_on=occurred_on, account=account,
                        )
                    ),
                    None,
                )
                if canonical is not None:
                    await self._attach_transaction_evidence(
                        user_id=user_id, transaction_id=canonical["id"], source=source,
                        source_ref=source_ref, evidence_scheme=evidence_scheme,
                        evidence_key=evidence_key, metadata=metadata, captured_at=now,
                    )
                    if canonical["status"] == "pending":
                        await self._conn.execute(
                            "UPDATE transactions SET status='confirmed',confirmed_at=?,updated_at=? "
                            "WHERE id=?",
                            (now, now, canonical["id"]),
                        )
                        if canonical["ledger_role"] != "self_transfer_principal":
                            await self._conn.execute(
                                "INSERT OR IGNORE INTO sync_outbox "
                                "(transaction_id,operation,created_at,updated_at) VALUES (?,'upsert',?,?)",
                                (canonical["id"], now, now),
                            )
                        await self._conn.execute(
                            "INSERT INTO transaction_events(transaction_id,event_type,metadata_json,created_at) "
                            "VALUES (?,'confirmed_by_evidence',?,?)",
                            (canonical["id"], json.dumps({"source": source}, ensure_ascii=False), now),
                        )
                    await self._conn.commit()
                    row = await (await self._conn.execute(
                        "SELECT * FROM transactions WHERE id=?", (canonical["id"],)
                    )).fetchone()
                    return dict(row), False
                if candidates:
                    evidence_conflict_with = candidates[0]["id"]
            tx_id = str(uuid.uuid4())
            await self._conn.execute(
                "INSERT INTO transactions "
                "(id,user_id,kind,ledger_role,transfer_bundle_id,transfer_leg,status,amount_idr,occurred_on,description,"
                "merchant,category,subcategory,account,source,source_ref,"
                "recurring_page_id,created_at,updated_at,confirmed_at) "
                "VALUES (?,?,?,?,?,?, 'confirmed',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx_id,
                    user_id,
                    kind,
                    ledger_role,
                    transfer_bundle_id,
                    transfer_leg,
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
            event_metadata = dict(metadata or {})
            if evidence_conflict_with:
                event_metadata["evidence_conflict_with"] = evidence_conflict_with
            await self._attach_transaction_evidence(
                user_id=user_id, transaction_id=tx_id, source=source,
                source_ref=source_ref, evidence_scheme=evidence_scheme,
                evidence_key=evidence_key, metadata=metadata, captured_at=now,
            )
            await self._conn.execute(
                "INSERT INTO transaction_events "
                "(transaction_id,event_type,metadata_json,created_at) "
                "VALUES (?,'confirmed_external',?,?)",
                (
                    tx_id,
                    json.dumps(event_metadata, ensure_ascii=False),
                    now,
                ),
            )
            if evidence_conflict_with:
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) VALUES (?,'evidence_conflict',?,?)",
                    (evidence_conflict_with, json.dumps({"conflict_transaction_id": tx_id}, ensure_ascii=False), now),
                )
                await self._conn.execute(
                    "UPDATE transactions SET status='pending',confirmed_at=NULL WHERE id=?",
                    (tx_id,),
                )
                await self._conn.execute(
                    "INSERT INTO transaction_events "
                    "(transaction_id,event_type,metadata_json,created_at) "
                    "VALUES (?,'evidence_conflict',?,?)",
                    (
                        tx_id,
                        json.dumps({"conflict_with": evidence_conflict_with}, ensure_ascii=False),
                        now,
                    ),
                )
            if ledger_role != "self_transfer_principal" and evidence_conflict_with is None:
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
            if row["ledger_role"] == "self_transfer_principal":
                # Principal legs are authoritative ledger rows, not ordinary
                # Notion expense/income rows. Clean up any legacy open job
                # rather than allowing confirmation to publish one.
                await self._conn.execute(
                    "DELETE FROM sync_outbox WHERE transaction_id=? AND completed_at IS NULL",
                    (transaction_id,),
                )
            else:
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
        require_expected_revision: bool = False,
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
            if row["ledger_role"] == "self_transfer_principal":
                raise SelfTransferMutationError(
                    "Self-transfer principal legs cannot be edited independently; "
                    "mutate the transfer bundle instead"
                )
            if row["status"] != "confirmed":
                raise ValueError("Only confirmed transactions can be updated")
            if require_expected_revision and expected_updated_at is None:
                await self._conn.rollback()
                raise TransactionPreconditionRequiredError(
                    "Transaction revision is required"
                )
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
        require_expected_revision: bool = False,
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
            if row["ledger_role"] == "self_transfer_principal":
                raise SelfTransferMutationError(
                    "Self-transfer principal legs cannot be voided independently; "
                    "mutate the transfer bundle instead"
                )
            if row["status"] == "voided":
                await self._conn.rollback()
                return dict(row), False
            if (
                require_expected_revision
                and row["status"] == "confirmed"
                and expected_updated_at is None
            ):
                await self._conn.rollback()
                raise TransactionPreconditionRequiredError(
                    "Transaction revision is required"
                )
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
