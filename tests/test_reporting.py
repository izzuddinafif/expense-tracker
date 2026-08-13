from datetime import datetime, timezone

import pytest

from db import Database
from reporting import LedgerReporting


async def _db(tmp_path):
    database = await Database.connect(str(tmp_path / "ledger.db"))
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("a", 7, "expense", "confirmed", 120000, "2026-07-02", "Lunch, office", "Cafe_%", "Food", "Dining", "Jago", "test", now, now),
        ("b", 7, "income", "confirmed", 5000000, "2026-07-03", "Gaji 日本", "Employer", "Salary", "Salary", "BSI", "test", now, now),
        ("c", 7, "expense", "voided", 999999, "2026-07-04", "Hidden", "Hidden", "Food", "Dining", "Jago", "test", now, now),
        ("d", 7, "expense", "pending", 888888, "2026-07-05", "Pending", "Pending", "Food", "Dining", "Jago", "test", now, now),
        ("e", 8, "expense", "confirmed", 400, "2026-07-02", "Other", "Other", "Food", "Dining", "Jago", "test", now, now),
    ]
    await database._conn.executemany(
        "INSERT INTO transactions (id,user_id,kind,status,amount_idr,occurred_on,description,merchant,category,subcategory,account,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    await database._conn.commit()
    return database


@pytest.mark.asyncio
async def test_search_scope_status_and_literal_wildcards(tmp_path):
    db = await _db(tmp_path)
    try:
        report = LedgerReporting(db)
        assert [r["id"] for r in await report.search(7)] == ["b", "a"]
        assert [r["id"] for r in await report.search(7, kind="expense")] == ["a"]
        assert [r["id"] for r in await report.search(7, "Cafe_%")] == ["a"]
        assert await report.search(7, "Hidden") == []
        assert await report.search(8, "Lunch") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_summary_totals_and_unicode_csv(tmp_path):
    db = await _db(tmp_path)
    try:
        report = LedgerReporting(db)
        summary = await report.monthly_summary(7, "2026-07")
        assert summary["expense"]["total_idr"] == 120000
        assert summary["income"]["total_idr"] == 5000000
        assert summary["expense"]["by_category"] == {"Dining": 120000}
        assert summary["expense"]["biggest"]["description"] == "Lunch, office"
        payload = await report.export_csv(7, "2026-07")
        text = payload.decode("utf-8-sig")
        assert "Lunch, office" in text and "Gaji 日本" in text
        assert "Hidden" not in text and "Pending" not in text
        context = await report.expense_context(7)
        assert context == [
            {
                "description": "Lunch, office",
                "amount": 120000,
                "date": "2026-07-02",
                "subcategory": "Dining",
                "merchant": "Cafe_%",
                "account": "Jago",
            }
        ]
        assert await report.recent_expenses(7) == list(reversed(context))
        searched = await report.search_expense_context(7, "Lunch")
        assert searched[0]["description"] == "Lunch, office"
        assert searched[0]["amount"] == 120000
        assert await report.duplicate_descriptions(
            7, 120000.0, "2026-07-02"
        ) == ["Lunch, office"]
        assert await report.duplicate_descriptions(
            8, 120000, "2026-07-02"
        ) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_similar_merchant_is_scoped_by_date_amount_user_and_status(tmp_path):
    db = await _db(tmp_path)
    try:
        report = LedgerReporting(db)
        matches = await report.similar_by_merchant(
            7, "Cafe", 125000, "2026-07-20"
        )
        assert [row["description"] for row in matches] == ["Lunch, office"]
        assert await report.similar_by_merchant(
            8, "Cafe", 125000, "2026-07-20"
        ) == []
        assert await report.similar_by_merchant(
            7, "Cafe", 500000, "2026-07-20"
        ) == []
        symmetric = await report.similar_by_merchant(
            7, "Cafe_% Surabaya", 120000.0, "2026-07-20"
        )
        assert [row["description"] for row in symmetric] == ["Lunch, office"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_month_validation(tmp_path):
    db = await _db(tmp_path)
    try:
        report = LedgerReporting(db)
        with pytest.raises(ValueError):
            await report.monthly_summary(7, "2026-7")
        with pytest.raises(ValueError):
            await report.export_csv(7, "2026-13")
    finally:
        await db.close()
