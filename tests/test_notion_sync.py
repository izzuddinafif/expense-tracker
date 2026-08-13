import asyncio
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from db import Database
from models import ExpenseEntry, IncomeEntry
from notion_sync import NotionSyncWorker


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def token_encryption_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())


class FakeNotion:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.expenses = []
        self.incomes = []

    async def load_cache(self):
        return object()

    async def log_expense(self, entry, owner, cache, recurring_page_url=None):
        if self.fail:
            raise RuntimeError("offline")
        self.expenses.append((entry, owner))
        return "https://notion.so/385c2adf84548161a518e2a4536f22b8"

    async def log_income(self, entry, owner, cache):
        self.incomes.append((entry, owner))
        return "https://notion.so/485c2adf84548161a518e2a4536f22b8"


async def make_db(tmp_path):
    db = await Database.connect(str(tmp_path / "sync.db"))
    await db.upsert_user(7, owner_name="Afif", notion_token="token")
    return db


async def queue(db, kind):
    entry_cls = ExpenseEntry if kind == "expense" else IncomeEntry
    entry = entry_cls(
        description=f"{kind} item", amount=12000, date="2026-01-01",
        subcategory="Other", account="Cash", confidence=1,
        **({"merchant": "Shop"} if kind == "expense" else {}),
    )
    if kind == "expense":
        await db.set_pending_expense(7, entry)
        return await db.confirm_pending_expense(7)
    await db.set_pending_income(7, entry)
    return await db.confirm_pending_income(7)


@pytest.mark.asyncio
async def test_dispatches_expense_and_income_and_does_not_redeliver(tmp_path):
    db = await make_db(tmp_path)
    await queue(db, "expense")
    await queue(db, "income")
    fake = FakeNotion()
    worker = NotionSyncWorker(db, client_factory=lambda user: fake, clock=lambda: NOW)

    assert await worker.run_once() == 2
    assert len(fake.expenses) == len(fake.incomes) == 1
    assert await worker.run_once() == 0
    rows = await db.list_transactions(7)
    assert all(row["notion_page_id"] for row in rows)
    await db.close()


@pytest.mark.asyncio
async def test_failure_is_isolated_and_exponential_backoff_is_bounded(tmp_path):
    db = await make_db(tmp_path)
    await queue(db, "expense")
    await queue(db, "income")
    failing, healthy = FakeNotion(fail=True), FakeNotion()
    calls = 0

    def factory(user):
        nonlocal calls
        calls += 1
        return failing if calls == 1 else healthy

    worker = NotionSyncWorker(
        db, client_factory=factory, clock=lambda: NOW, jitter=lambda a, b: 0,
        base_delay=30, max_delay=30,
    )
    assert await worker.run_once() == 2
    row = await (await db._conn.execute(
        "SELECT attempt_count,last_error,next_attempt_at,completed_at "
        "FROM sync_outbox ORDER BY id LIMIT 1"
    )).fetchone()
    assert row["attempt_count"] == 1
    assert "offline" in row["last_error"]
    assert row["next_attempt_at"] == "2026-01-01T00:00:30+00:00"
    assert row["completed_at"] is None
    assert len(healthy.incomes) == 1
    await db.close()


@pytest.mark.asyncio
async def test_unsupported_transfer_and_archive_are_recorded(tmp_path):
    db = await make_db(tmp_path)
    tx_id = await queue(db, "expense")
    await db._conn.execute("UPDATE transactions SET kind='transfer' WHERE id=?", (tx_id,))
    await db._conn.execute("UPDATE sync_outbox SET operation='archive' WHERE transaction_id=?", (tx_id,))
    await db._conn.commit()
    worker = NotionSyncWorker(db, client_factory=lambda user: FakeNotion(), clock=lambda: NOW)

    await worker.run_once()
    row = await (await db._conn.execute(
        "SELECT attempt_count,last_error FROM sync_outbox WHERE transaction_id=?", (tx_id,)
    )).fetchone()
    assert row["attempt_count"] == 1
    assert "unsupported" in row["last_error"]
    await db.close()


@pytest.mark.asyncio
async def test_loop_propagates_cancellation():
    async def cancel_sleep(_):
        raise asyncio.CancelledError

    class EmptyDb:
        async def list_due_notion_sync_jobs(self, now, limit):
            return []

    worker = NotionSyncWorker(EmptyDb(), sleep=cancel_sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.run()
