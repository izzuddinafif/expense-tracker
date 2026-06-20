import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from models import ExpenseEntry, EmailTransaction, IncomeEntry, UserRecord

log = logging.getLogger(__name__)


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, path: str) -> "Database":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        db = cls(conn)
        await db._init()
        await db._migrate_schema()
        await db._migrate_from_json(path, conn)
        return db

    async def close(self) -> None:
        await self._conn.close()

    # ── Schema ──────────────────────────────────────────────────────────────────

    async def _init(self) -> None:
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
        """)

    async def _migrate_schema(self) -> None:
        """Add new columns to existing tables for backward compatibility."""
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
            await self._conn.execute(
                "INSERT INTO users (telegram_id, owner_name, notion_token, setup_step, created_at, updated_at) "
                "VALUES (?, ?, ?, 'migrated', ?, ?)",
                (uid, name, notion_token, now, now),
            )
        await self._conn.commit()
        log.info(f"Migrated {len(users)} user(s) from env vars — run /setup to discover databases")

    # ── Processed emails ────────────────────────────────────────────────────────

    async def is_processed(self, uid: str) -> bool:
        cur = await self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE uid = ?", (uid,)
        )
        return await cur.fetchone() is not None

    async def mark_processed(self, uid: str, sender: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO processed_emails (uid, sender, processed_at) VALUES (?, ?, ?)",
            (uid, sender, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    async def get_all_processed_uids(self) -> set[str]:
        cur = await self._conn.execute("SELECT uid FROM processed_emails")
        rows = await cur.fetchall()
        return {row["uid"] for row in rows}

    async def prune_processed(self, days: int = 90) -> int:
        cutoff_dt = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self._conn.execute(
            "DELETE FROM processed_emails WHERE processed_at < ?", (cutoff_dt,)
        )
        await self._conn.commit()
        return cur.rowcount

    # ── Pending expense (one per user) ──────────────────────────────────────────

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

    async def clear_pending_expense(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_expenses WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Pending income (one per user) ──────────────────────────────────────────

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

    async def clear_pending_income(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_income WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Pending email expense (current debit card follow-up) ────────────────────

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

    async def clear_pending_email_expense(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM pending_email_expenses WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Debit queue (FIFO per user) ─────────────────────────────────────────────

    async def push_debit(self, user_id: int, tx: EmailTransaction) -> None:
        await self._conn.execute(
            "INSERT INTO pending_debit_queue (user_id, tx_json, created_at) VALUES (?, ?, ?)",
            (user_id, tx.model_dump_json(), datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

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

    async def set_debit_merchant(self, user_id: int, amount: float, description: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO debit_merchant_cache (user_id, amount, description, created_at) VALUES (?, ?, ?, ?)",
            (user_id, int(round(amount)), description, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

    # ── Conversation history ────────────────────────────────────────────────────

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
            "SELECT role, content FROM conversation_history "
            "WHERE user_id = ? ORDER BY created_at ASC, id ASC LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    async def clear_history(self, user_id: int) -> None:
        await self._conn.execute(
            "DELETE FROM conversation_history WHERE user_id = ?", (user_id,)
        )
        await self._conn.commit()

    # ── Pending since (auto-confirm timestamps) ──────────────────────────────

    async def set_pending_since(self, user_id: int, timestamp: float) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO pending_since (user_id, created_at) VALUES (?, ?)",
            (user_id, timestamp),
        )
        await self._conn.commit()

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

    async def set_email_account_owner(self, account_pattern: str, telegram_id: int) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO email_account_owners (account_pattern, telegram_id, created_at) VALUES (?, ?, ?)",
            (account_pattern, telegram_id, datetime.now(timezone.utc).isoformat()),
        )
        await self._conn.commit()

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
        return UserRecord(
            telegram_id=row["telegram_id"],
            owner_name=row["owner_name"],
            notion_token=row["notion_token"],
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
        return UserRecord(
            telegram_id=row["telegram_id"],
            owner_name=row["owner_name"],
            notion_token=row["notion_token"],
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

    async def get_all_users(self) -> dict[int, UserRecord]:
        cur = await self._conn.execute("SELECT * FROM users")
        rows = await cur.fetchall()
        result: dict[int, UserRecord] = {}
        for row in rows:
            result[row["telegram_id"]] = UserRecord(
                telegram_id=row["telegram_id"],
                owner_name=row["owner_name"],
                notion_token=row["notion_token"],
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
        return result
