from datetime import datetime, timezone

import pytest

from db import Database
from local_budgets import BudgetStore


@pytest.mark.asyncio
async def test_budget_crud_report_and_user_scope(tmp_path):
    db = await Database.connect(str(tmp_path / "budgets.db"))
    store = BudgetStore(db)
    await store.initialize()
    migration = await db._conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=6"
    )
    assert await migration.fetchone() is not None
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.executemany(
        "INSERT INTO transactions "
        "(id,user_id,kind,status,amount_idr,occurred_on,description,"
        "category,subcategory,account,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("a", 7, "expense", "confirmed", 80_000, "2026-07-01", "Lunch", "Food", "Dining", "Jago", "test", now, now),
            ("b", 7, "expense", "confirmed", 25_000, "2026-07-02", "Dinner", "Food", "Dining", "Jago", "test", now, now),
            ("c", 7, "expense", "voided", 999_000, "2026-07-03", "Void", "Food", "Dining", "Jago", "test", now, now),
            ("d", 8, "expense", "confirmed", 999_000, "2026-07-01", "Other", "Food", "Dining", "Jago", "test", now, now),
            ("e", 7, "income", "confirmed", 999_000, "2026-07-04", "Income", "Food", "Dining", "Jago", "test", now, now),
            ("f", 7, "expense", "pending", 999_000, "2026-07-05", "Pending", "Food", "Dining", "Jago", "test", now, now),
            ("g", 7, "expense", "confirmed", 999_000, "2026-06-30", "Old", "Food", "Dining", "Jago", "test", now, now),
        ],
    )
    await db._conn.commit()
    try:
        await store.set(7, "2026-07", "Dining", 100_000)
        report = await store.report(7, "2026-07")
        assert report[0]["spent_idr"] == 105_000
        assert report[0]["remaining_idr"] == -5_000
        assert report[0]["status"] == "over"
        assert report[0]["name"] == "Dining"
        assert report[0]["budget"] == 100_000
        assert report[0]["spent"] == 105_000
        assert report[0]["period"] == "2026-07"
        assert report[0]["subcategories"] == ["Dining"]
        assert await store.report(8, "2026-07") == []
        assert await store.delete(7, "2026-07", "dining")
        assert await store.list(7, "2026-07") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_budget_report_fuzzy_subcategory_and_warning(tmp_path):
    db = await Database.connect(str(tmp_path / "budgets.db"))
    store = BudgetStore(db)
    await store.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO transactions "
        "(id,user_id,kind,status,amount_idr,occurred_on,description,"
        "category,subcategory,account,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "fuzzy",
            7,
            "expense",
            "confirmed",
            85_000,
            "2026-07-20",
            "Nasi goreng",
            "Food",
            "Warung/Makan Siap Saji",
            "Cash",
            "test",
            now,
            now,
        ),
    )
    await db._conn.commit()
    try:
        await store.set(7, "2026-07", "warung", 100_000)
        report = await store.report(7, "2026-07")
        assert report[0]["spent_idr"] == 85_000
        assert report[0]["percentage"] == 85
        assert report[0]["status"] == "warning"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_overlapping_budget_labels_assign_an_expense_once(tmp_path):
    db = await Database.connect(str(tmp_path / "budgets.db"))
    store = BudgetStore(db)
    await store.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO transactions "
        "(id,user_id,kind,status,amount_idr,occurred_on,description,"
        "category,subcategory,account,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "overlap",
            7,
            "expense",
            "confirmed",
            50_000,
            "2026-07-20",
            "Nasi goreng",
            "Food",
            "Food delivery",
            "Cash",
            "test",
            now,
            now,
        ),
    )
    await db._conn.commit()
    try:
        await store.set(7, "2026-07", "Food", 100_000)
        await store.set(7, "2026-07", "Food delivery", 100_000)
        report = await store.report(7, "2026-07")
        assert {row["category"]: row["spent_idr"] for row in report} == {
            "Food": 0,
            "Food delivery": 50_000,
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_budget_uses_category_when_subcategory_differs_and_avoids_substring_collision(tmp_path):
    db = await Database.connect(str(tmp_path / "budgets.db"))
    store = BudgetStore(db)
    await store.initialize()
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.executemany(
        "INSERT INTO transactions "
        "(id,user_id,kind,status,amount_idr,occurred_on,description,"
        "category,subcategory,account,source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("category-fallback", 7, "expense", "confirmed", 40_000, "2026-07-20", "Lunch", "Food", "Dining", "Cash", "test", now, now),
            ("collision", 7, "expense", "confirmed", 60_000, "2026-07-21", "Card fee", "Fees", "Card fee", "Cash", "test", now, now),
        ],
    )
    await db._conn.commit()
    try:
        await store.set(7, "2026-07", "Food", 100_000)
        await store.set(7, "2026-07", "Car", 100_000)
        report = {row["category"]: row for row in await store.report(7, "2026-07")}
        assert report["Food"]["spent_idr"] == 40_000
        assert report["Car"]["spent_idr"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_budget_validation_and_upsert(tmp_path):
    db = await Database.connect(str(tmp_path / "budgets.db"))
    store = BudgetStore(db)
    await store.initialize()
    try:
        await store.set(7, "2026-07", "Transport", 200_000)
        await store.set(7, "2026-07", "transport", 300_000)
        rows = await store.list(7, "2026-07")
        assert len(rows) == 1
        assert rows[0]["amount_idr"] == 300_000
        with pytest.raises(ValueError):
            await store.set(7, "2026-7", "Food", 1)
        with pytest.raises(ValueError):
            await store.set(7, "2026-07", "", 1)
        with pytest.raises(ValueError):
            await store.set(7, "2026-07", "Food", 0)
    finally:
        await db.close()
