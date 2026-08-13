"""Pure command handling for the local monthly budget feature."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Callable

from reporting_views import format_budget_report

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_MONTH_SHAPE_RE = re.compile(r"^\d{4}-.+$")


@dataclass(frozen=True)
class BudgetCommandResult:
    text: str
    parse_mode: str | None = None


class BudgetCommandService:
    """Execute ``/budget`` commands against a BudgetStore-like object."""

    def __init__(self, store: Any, amount_parser: Callable[[str], Any]) -> None:
        self.store = store
        self.amount_parser = amount_parser

    async def execute(self, user_id: int, raw: str, today: date | None = None) -> BudgetCommandResult:
        parts = (raw or "").split()
        token = parts[0].lower() if parts else ""
        if not (token == "/budget" or token.startswith("/budget@")):
            raise ValueError("Gunakan /budget, /budget YYYY-MM, /budget set ..., atau /budget delete ....")
        args = parts[1:]
        current_month = (today or date.today()).strftime("%Y-%m")
        if args and args[0].lower() == "set":
            return await self._set(user_id, args[1:], current_month)
        if args and args[0].lower() == "delete":
            return await self._delete(user_id, args[1:], current_month)
        if len(args) > 1:
            raise ValueError("Gunakan /budget, /budget YYYY-MM, /budget set ..., atau /budget delete ....")
        month = self._month(args[0]) if args else current_month
        rows = await self.store.report(user_id, month)
        return BudgetCommandResult(format_budget_report(rows, month), "Markdown")

    @staticmethod
    def _month(value: str) -> str:
        if _MONTH_RE.fullmatch(value or "") is None:
            raise ValueError("Bulan harus memakai format YYYY-MM.")
        return value

    def _optional_month(self, args: list[str]) -> tuple[str, int]:
        if args and _MONTH_SHAPE_RE.fullmatch(args[0] or ""):
            return self._month(args[0]), 1
        return "", 0

    async def _set(self, user_id: int, args: list[str], current_month: str) -> BudgetCommandResult:
        if len(args) < 2:
            raise ValueError("Gunakan /budget set <jumlah> <kategori> atau /budget set YYYY-MM <jumlah> <kategori>.")
        month, index = self._optional_month(args)
        month = month or current_month
        if len(args) - index < 2:
            raise ValueError("Gunakan /budget set <jumlah> <kategori> atau /budget set YYYY-MM <jumlah> <kategori>.")
        amount_text, category = args[index], " ".join(args[index + 1:]).strip()
        try:
            amount = self.amount_parser(amount_text)
        except (TypeError, ValueError) as exc:
            raise ValueError("Jumlah tidak valid.") from exc
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or int(amount) != amount or amount <= 0:
            raise ValueError("Budget IDR harus berupa rupiah bulat.")
        await self.store.set(user_id, month, category, int(amount))
        return BudgetCommandResult(f"✅ Budget {category} untuk {month} disimpan: Rp {int(amount):,}.")

    async def _delete(self, user_id: int, args: list[str], current_month: str) -> BudgetCommandResult:
        if not args:
            raise ValueError("Gunakan /budget delete <kategori> atau /budget delete YYYY-MM <kategori>.")
        month, index = self._optional_month(args)
        month = month or current_month
        category = " ".join(args[index:]).strip()
        if not category:
            raise ValueError("Gunakan /budget delete <kategori> atau /budget delete YYYY-MM <kategori>.")
        deleted = await self.store.delete(user_id, month, category)
        status = "✅ Dihapus" if deleted else "ℹ️ Tidak ditemukan"
        return BudgetCommandResult(f"{status}: budget {category} untuk {month}.")
