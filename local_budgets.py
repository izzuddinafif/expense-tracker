"""SQLite-backed monthly budget definitions for the personal ledger."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _month(value: str) -> str:
    if not isinstance(value, str) or _MONTH_RE.fullmatch(value) is None:
        raise ValueError("month must use YYYY-MM format")
    return value


def _category(value: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 100:
        raise ValueError("category must contain 1-100 characters")
    return value


def _matches_category(expected: str, actual: str) -> bool:
    """Match explicit token sequences without substring collisions."""
    expected_key = expected.strip().casefold()
    actual_key = actual.strip().casefold()
    if not expected_key or not actual_key:
        return False
    expected_tokens = re.findall(r"[a-z0-9]+", expected_key)
    actual_tokens = re.findall(r"[a-z0-9]+", actual_key)
    if not expected_tokens or not actual_tokens:
        return False
    width = len(expected_tokens)
    reverse_width = len(actual_tokens)
    return (
        expected_tokens == actual_tokens
        or any(
            actual_tokens[index : index + width] == expected_tokens
            for index in range(len(actual_tokens) - width + 1)
        )
        or any(
            expected_tokens[index : index + reverse_width] == actual_tokens
            for index in range(len(expected_tokens) - reverse_width + 1)
        )
    )


def _category_match_score(expected: str, actual: str) -> tuple[int, int] | None:
    """Return a deterministic specificity score for one budget candidate."""
    if not _matches_category(expected, actual):
        return None
    expected_key = expected.strip().casefold()
    actual_key = actual.strip().casefold()
    exact = int(expected_key == actual_key)
    return exact, len(expected_key)


class BudgetStore:
    """Small budget repository sharing the application's SQLite connection."""

    def __init__(self, database: Any) -> None:
        self._conn = database._conn
        self._write_lock = database._write_lock

    async def initialize(self) -> None:
        """Ensure the v6 table exists for lightweight/fake Database adapters."""
        async with self._write_lock:
            await self._conn.execute(
                "CREATE TABLE IF NOT EXISTS monthly_budgets ("
                "user_id INTEGER NOT NULL,"
                "month TEXT NOT NULL,"
                "category TEXT NOT NULL COLLATE NOCASE,"
                "amount_idr INTEGER NOT NULL CHECK(amount_idr > 0),"
                "created_at TEXT NOT NULL,"
                "updated_at TEXT NOT NULL,"
                "PRIMARY KEY(user_id,month,category))"
            )
            await self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_monthly_budgets_user_month "
                "ON monthly_budgets(user_id,month)"
            )
            await self._conn.commit()

    async def set(
        self, user_id: int, month: str, category: str, amount_idr: int
    ) -> dict[str, Any]:
        month = _month(month)
        category = _category(category)
        if isinstance(amount_idr, bool) or not isinstance(amount_idr, int) or amount_idr <= 0:
            raise ValueError("budget amount must be a positive integer IDR value")
        now = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            await self._conn.execute(
                "INSERT INTO monthly_budgets "
                "(user_id,month,category,amount_idr,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,month,category) DO UPDATE "
                "SET amount_idr=excluded.amount_idr,updated_at=excluded.updated_at",
                (user_id, month, category, amount_idr, now, now),
            )
            await self._conn.commit()
            cur = await self._conn.execute(
                "SELECT * FROM monthly_budgets "
                "WHERE user_id=? AND month=? AND category=?",
                (user_id, month, category),
            )
            return dict(await cur.fetchone())

    async def delete(self, user_id: int, month: str, category: str) -> bool:
        async with self._write_lock:
            cur = await self._conn.execute(
                "DELETE FROM monthly_budgets WHERE user_id=? AND month=? AND category=?",
                (user_id, _month(month), _category(category)),
            )
            await self._conn.commit()
            return cur.rowcount > 0

    async def list(self, user_id: int, month: str) -> list[dict[str, Any]]:
        cur = await self._conn.execute(
            "SELECT * FROM monthly_budgets WHERE user_id=? AND month=? "
            "ORDER BY category COLLATE NOCASE",
            (user_id, _month(month)),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def report(self, user_id: int, month: str) -> list[dict[str, Any]]:
        """Return budget usage from confirmed authoritative expenses."""
        month = _month(month)
        definitions = await self.list(user_id, month)
        cur = await self._conn.execute(
            "SELECT category,subcategory,amount_idr FROM transactions "
            "WHERE user_id=? AND status='confirmed' AND kind='expense' "
            "AND substr(occurred_on,1,7)=?",
            (user_id, month),
        )
        expenses = [dict(row) for row in await cur.fetchall()]
        spent_by_budget = [0] * len(definitions)
        for expense in expenses:
            actual_values = [
                value for value in (expense["category"], expense["subcategory"])
                if value
            ]
            candidates = [
                (score, index)
                for index, budget in enumerate(definitions)
                for actual in actual_values
                if (score := _category_match_score(budget["category"], actual)) is not None
            ]
            if candidates:
                # An exact label wins; otherwise the most specific matching
                # label owns the expense. This prevents parent and child
                # substring budgets from double-counting one transaction.
                _, selected = max(candidates, key=lambda item: (item[0][0], item[0][1], -item[1]))
                spent_by_budget[selected] += int(expense["amount_idr"])
        result: list[dict[str, Any]] = []
        for index, budget in enumerate(definitions):
            category = budget["category"]
            spent = spent_by_budget[index]
            amount = int(budget["amount_idr"])
            percentage = spent / amount * 100
            status = "over" if percentage >= 100 else (
                "warning" if percentage >= 80 else "ok"
            )
            result.append(
                {
                    "month": month,
                    "period": month,
                    "category": category,
                    "name": category,
                    "budget_idr": amount,
                    "budget": amount,
                    "spent_idr": spent,
                    "spent": spent,
                    "remaining_idr": amount - spent,
                    "percentage": percentage,
                    "status": status,
                    "subcategories": [category],
                }
            )
        return result
