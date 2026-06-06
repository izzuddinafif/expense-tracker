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
        await db._migrate_from_json(path, conn)
        return db

    async def close(self) -> None:
        await self._conn.close()

    # ── Schema ──────────────────────────────────────────────────────────────────

    async def _init(self) -> None:
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
        """Pop the oldest queued debit tx for this user. Returns None if empty."""
        cur = await self._conn.execute(
            "SELECT id, tx_json FROM pending_debit_queue "
            "WHERE user_id = ? ORDER BY id ASC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await self._conn.execute(
            "DELETE FROM pending_debit_queue WHERE id = ?", (row["id"],)
        )
        await self._conn.commit()
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
        now = datetime.now(timezone.utc).isoformat()
        # Check if user exists
        existing = await self.get_user(telegram_id)
        if existing is None:
            # Insert new user
            cols = ["telegram_id", "created_at", "updated_at"] + list(fields.keys())
            vals = [telegram_id, now, now] + list(fields.values())
            placeholders = ", ".join(["?"] * len(cols))
            col_names = ", ".join(cols)
            await self._conn.execute(
                f"INSERT INTO users ({col_names}) VALUES ({placeholders})",
                vals,
            )
        else:
            # Update existing user
            if fields:
                set_clause = ", ".join(f"{k} = ?" for k in fields)
                vals = list(fields.values()) + [now, telegram_id]
                await self._conn.execute(
                    f"UPDATE users SET {set_clause}, updated_at = ? WHERE telegram_id = ?",
                    vals,
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
