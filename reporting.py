"""Read-only reporting queries over the authoritative SQLite ledger.

The service intentionally depends only on ``Database._conn`` so it can be
used by Telegram, API, or command-line handlers without duplicating ledger
business rules.  Reports never include pending or voided transactions.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any


_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_SEARCH_COLUMNS = ("description", "merchant", "category", "subcategory", "account")
_CSV_COLUMNS = (
    "id", "occurred_on", "kind", "amount_idr", "description", "merchant",
    "category", "subcategory", "account", "source", "source_ref",
)


def validate_month(month: str | None) -> str | None:
    """Validate an optional YYYY-MM filter, rejecting dates like 2024-13."""
    if month is None or month == "":
        return None
    if not isinstance(month, str) or _MONTH_RE.fullmatch(month) is None:
        raise ValueError("month must use YYYY-MM format")
    return month


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _whole_idr(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or not float(value).is_integer()
    ):
        raise ValueError("amount_idr must be a positive whole IDR value")
    return int(value)


class LedgerReporting:
    """Async read-only reports backed by a :class:`db.Database` instance."""

    def __init__(self, database: Any) -> None:
        self._conn = database._conn

    async def search(
        self,
        user_id: int,
        query: str = "",
        *,
        limit: int = 50,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search confirmed transactions for one user across text fields."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if kind not in {None, "expense", "income", "transfer"}:
            raise ValueError("invalid transaction kind")
        query = str(query or "").strip()
        params: list[Any] = [user_id]
        where = "user_id = ? AND status = 'confirmed'"
        if kind is not None:
            where += " AND kind = ?"
            params.append(kind)
        if query:
            pattern = f"%{_escape_like(query)}%"
            where += " AND (" + " OR ".join(
                f"lower({column}) LIKE lower(?) ESCAPE '\\'" for column in _SEARCH_COLUMNS
            ) + ")"
            params.extend([pattern] * len(_SEARCH_COLUMNS))
        params.append(limit)
        cursor = await self._conn.execute(
            f"SELECT * FROM transactions WHERE {where} "
            "ORDER BY occurred_on DESC, created_at DESC, id DESC LIMIT ?", params
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def monthly_summary(self, user_id: int, month: str) -> dict[str, Any]:
        """Return integer-IDR totals and category/account breakdowns."""
        month = validate_month(month)
        assert month is not None
        cursor = await self._conn.execute(
            "SELECT kind,amount_idr,occurred_on,description,category,"
            "subcategory,account FROM transactions "
            "WHERE user_id=? AND status='confirmed' AND substr(occurred_on,1,7)=?",
            (user_id, month),
        )
        rows = await cursor.fetchall()
        result: dict[str, Any] = {"month": month}
        for kind in ("expense", "income"):
            selected = [row for row in rows if row["kind"] == kind]
            by_category: dict[str, int] = {}
            by_account: dict[str, int] = {}
            for row in selected:
                category = row["subcategory"] or row["category"] or "Uncategorized"
                account = row["account"] or "Unspecified"
                by_category[category] = by_category.get(category, 0) + int(row["amount_idr"])
                by_account[account] = by_account.get(account, 0) + int(row["amount_idr"])
            result[kind] = {
                "total_idr": sum(int(row["amount_idr"]) for row in selected),
                "count": len(selected),
                "by_category": dict(sorted(by_category.items())),
                "by_account": dict(sorted(by_account.items())),
            }
            if kind == "expense":
                biggest = max(selected, key=lambda row: row["amount_idr"], default=None)
                result[kind]["biggest"] = (
                    {
                        "amount_idr": int(biggest["amount_idr"]),
                        "description": biggest["description"],
                        "occurred_on": biggest["occurred_on"],
                    }
                    if biggest is not None
                    else None
                )
        result["transfer_count"] = sum(1 for row in rows if row["kind"] == "transfer")
        return result

    async def expense_context(
        self, user_id: int, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Return recent expenses in the legacy LLM query-compatible shape."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        cursor = await self._conn.execute(
            "SELECT description,amount_idr,occurred_on,subcategory,category,"
            "merchant,account FROM transactions "
            "WHERE user_id=? AND status='confirmed' AND kind='expense' "
            "ORDER BY occurred_on DESC,created_at DESC,id DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "description": row["description"],
                "amount": int(row["amount_idr"]),
                "date": row["occurred_on"],
                "subcategory": row["subcategory"] or row["category"],
                "merchant": row["merchant"],
                "account": row["account"],
            }
            for row in reversed(rows)
        ]

    async def recent_expenses(
        self, user_id: int, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Return newest-first extraction context in the legacy Notion shape."""
        context = await self.expense_context(user_id, limit=limit)
        return list(reversed(context))

    async def search_expense_context(
        self, user_id: int, query: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Return description matches in the legacy extraction-compatible shape."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        pattern = f"%{_escape_like(str(query or '').strip())}%"
        cursor = await self._conn.execute(
            "SELECT * FROM transactions WHERE user_id=? AND status='confirmed' "
            "AND kind='expense' AND lower(description) LIKE lower(?) ESCAPE '\\' "
            "ORDER BY occurred_on DESC,created_at DESC,id DESC LIMIT ?",
            (user_id, pattern, limit),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        return [
            {
                "description": row["description"],
                "amount": int(row["amount_idr"]),
                "date": row["occurred_on"],
                "subcategory": row["subcategory"] or row["category"],
                "merchant": row["merchant"],
                "account": row["account"],
            }
            for row in rows
        ]

    async def duplicate_descriptions(
        self,
        user_id: int,
        amount_idr: int | float,
        occurred_on: str,
        *,
        kind: str = "expense",
    ) -> list[str]:
        """Find same-date, near-identical-amount candidates for LLM review."""
        if kind not in {"expense", "income"}:
            raise ValueError("kind must be expense or income")
        amount_idr = _whole_idr(amount_idr)
        date.fromisoformat(occurred_on)
        cursor = await self._conn.execute(
            "SELECT description FROM transactions WHERE user_id=? "
            "AND status='confirmed' AND kind=? AND occurred_on=? "
            "AND amount_idr BETWEEN ? AND ? "
            "ORDER BY created_at DESC,id DESC",
            (user_id, kind, occurred_on, amount_idr - 1, amount_idr + 1),
        )
        return [row["description"] for row in await cursor.fetchall()]

    async def similar_by_merchant(
        self,
        user_id: int,
        merchant: str,
        amount_idr: int | float,
        occurred_on: str,
    ) -> list[dict[str, Any]]:
        """Find confirmed 90-day merchant matches within 20% of the amount."""
        merchant = str(merchant or "").strip()
        if not merchant:
            return []
        amount_idr = _whole_idr(amount_idr)
        transaction_date = date.fromisoformat(occurred_on)
        since = (transaction_date - timedelta(days=90)).isoformat()
        cursor = await self._conn.execute(
            "SELECT description,merchant,amount_idr,occurred_on,subcategory,"
            "category,account FROM transactions WHERE user_id=? "
            "AND status='confirmed' AND kind='expense' "
            "AND occurred_on BETWEEN ? AND ? "
            "ORDER BY occurred_on DESC,created_at DESC,id DESC",
            (user_id, since, occurred_on),
        )
        merchant_key = merchant.casefold()
        matches = []
        for row in await cursor.fetchall():
            actual = (row["merchant"] or row["description"] or "").strip()
            actual_key = actual.casefold()
            if not actual_key or (
                merchant_key not in actual_key and actual_key not in merchant_key
            ):
                continue
            if abs(int(row["amount_idr"]) - amount_idr) / max(amount_idr, 1) > 0.2:
                continue
            matches.append(row)
        return [
            {
                "description": row["description"],
                "merchant": row["merchant"],
                "amount": int(row["amount_idr"]),
                "date": row["occurred_on"],
                "subcategory": row["subcategory"] or row["category"],
                "account": row["account"],
            }
            for row in matches
        ]

    async def export_csv(
        self, user_id: int, month: str | None = None, *, include_bom: bool = True
    ) -> bytes:
        """Export confirmed rows as UTF-8 CSV bytes with stable ordering."""
        month = validate_month(month)
        params: list[Any] = [user_id]
        where = "user_id=? AND status='confirmed'"
        if month:
            where += " AND substr(occurred_on,1,7)=?"
            params.append(month)
        cursor = await self._conn.execute(
            f"SELECT {', '.join(_CSV_COLUMNS)} FROM transactions WHERE {where} "
            "ORDER BY occurred_on ASC, created_at ASC, id ASC", params
        )
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(_CSV_COLUMNS)
        for row in await cursor.fetchall():
            writer.writerow([row[column] if row[column] is not None else "" for column in _CSV_COLUMNS])
        text = output.getvalue()
        return ("\ufeff" + text if include_bom else text).encode("utf-8")


# Short alias for callers that prefer a service-style name.
ReportingService = LedgerReporting
