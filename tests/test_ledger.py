import pytest

from db import Database
from models import ExpenseEntry, IncomeEntry


@pytest.mark.asyncio
async def test_ledger_migration_and_atomic_confirmation(tmp_path):
    db = await Database.connect(str(tmp_path / "ledger.db"))
    try:
        row = await (await db._conn.execute("SELECT name FROM sqlite_master WHERE name='transactions'")).fetchone()
        assert row is not None
        cache_table = await (
            await db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='notion_cache_snapshots'"
            )
        ).fetchone()
        assert cache_table is not None
        versions = await (
            await db._conn.execute(
                "SELECT version FROM schema_migrations WHERE version IN (6,7) "
                "ORDER BY version"
            )
        ).fetchall()
        assert [row["version"] for row in versions] == [6, 7]
        assert (await (await db._conn.execute("PRAGMA foreign_keys")).fetchone())[0] == 1

        await db.set_pending_expense(7, ExpenseEntry(description="Lunch", amount=25000,
            date="2026-07-28", subcategory="Food", account="Cash", confidence=1))
        tx_id = await db.confirm_pending_expense(
            7,
            source_ref="email:42",
            recurring_page_id="recurring-page-1",
        )
        assert tx_id
        assert await db.get_pending_expense(7) is None
        tx = await (await db._conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,))).fetchone()
        assert tx["user_id"] == 7
        assert tx["amount_idr"] == 25000 and tx["status"] == "confirmed"
        assert tx["recurring_page_id"] == "recurring-page-1"
        assert (await (await db._conn.execute("SELECT COUNT(*) FROM sync_outbox")).fetchone())[0] == 1
        assert await db.confirm_pending_expense(7, source_ref="email:42") == tx_id
        await db.mark_notion_sync_success(tx_id, "notion-page-1")
        synced = await (
            await db._conn.execute(
                "SELECT notion_page_id FROM transactions WHERE id=?", (tx_id,)
            )
        ).fetchone()
        assert synced["notion_page_id"] == "notion-page-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_income_confirmation_and_sync_failure(tmp_path):
    db = await Database.connect(str(tmp_path / "ledger.db"))
    try:
        await db.set_pending_income(3, IncomeEntry(description="Salary", amount=3000000,
            date="2026-07-01", subcategory="Salary", account="Bank", confidence=1))
        tx_id = await db.confirm_pending_income(3)
        outbox = await (await db._conn.execute("SELECT id FROM sync_outbox WHERE transaction_id=?", (tx_id,))).fetchone()
        await db.mark_notion_sync_failure(outbox["id"], "offline")
        row = await (await db._conn.execute("SELECT attempt_count,last_error FROM sync_outbox WHERE id=?", (outbox["id"],))).fetchone()
        assert row["attempt_count"] == 1 and row["last_error"] == "offline"
        await db.mark_notion_sync_success(outbox["id"])
        assert (await (await db._conn.execute("SELECT completed_at FROM sync_outbox WHERE id=?", (outbox["id"],))).fetchone())[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_day_cross_source_ingestion_never_drops_distinct_transaction(tmp_path):
    db = await Database.connect(str(tmp_path / "ledger.db"))
    try:
        await db.set_pending_expense(
            7,
            ExpenseEntry(
                description="QRIS",
                amount=48_500,
                date="2026-07-28",
                subcategory="Food",
                account="Mandiri",
                confidence=1,
            ),
        )
        await db.confirm_pending_expense(
            7, source="bank_email", source_ref="gmail:42"
        )
        row, created = await db.create_ingested_transaction(
            7,
            kind="expense",
            amount_idr=48_500,
            occurred_on="2026-07-28",
            description="Livin notification",
            account="Mandiri",
            source_ref="android:abc",
        )
        assert created is True
        assert row["source"] == "android_notification"
        count = await (
            await db._conn.execute("SELECT COUNT(*) FROM transactions")
        ).fetchone()
        assert count[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_confirmed_external_transaction_is_atomic_and_idempotent(tmp_path):
    db = await Database.connect(str(tmp_path / "ledger.db"))
    try:
        kwargs = {
            "kind": "expense",
            "amount_idr": 75_500,
            "occurred_on": "2026-07-29",
            "description": "QRIS payment",
            "merchant": "Kopi Contoh",
            "subcategory": "Food",
            "account": "Mandiri",
            "source": "bank_email",
            "source_ref": "gmail:uid-42:expense",
            "metadata": {"email_uid": "uid-42"},
        }
        first, created = await db.create_confirmed_external_transaction(7, **kwargs)
        replay, replay_created = await db.create_confirmed_external_transaction(
            7, **kwargs
        )
        assert created is True
        assert replay_created is False
        assert replay["id"] == first["id"]
        assert first["status"] == "confirmed"
        outbox_count = await (
            await db._conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE transaction_id=?",
                (first["id"],),
            )
        ).fetchone()
        assert outbox_count[0] == 1
    finally:
        await db.close()
