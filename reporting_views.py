"""Pure Telegram presentation helpers for ledger reports.

These functions deliberately contain no database or Telegram dependencies so
they can be tested in isolation and reused by other front ends.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping


def escape_markdown(value: Any) -> str:
    """Escape Telegram Markdown v1 control characters in ledger text."""
    text = "-" if value is None or value == "" else str(value)
    return "".join("\\" + char if char in "\\_*[`" else char for char in text)


def _idr(value: Any) -> str:
    return f"{int(value or 0):,}"


def format_search_results(keyword: str, rows: list[Mapping[str, Any]], max_items: int = 10) -> str:
    """Render `/search` results, safely escaping user/ledger-provided text."""
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
        raise ValueError("max_items must be a positive integer")
    total = sum(int(row.get("amount_idr") or 0) for row in rows)
    lines = [
        f'🔍 *Hasil pencarian: "{escape_markdown(keyword)}"* '
        f"({len(rows)} transaksi, Rp {_idr(total)})\n"
    ]
    for row in rows[:max_items]:
        lines.append(
            f"• Rp {_idr(row.get('amount_idr'))} — "
            f"{escape_markdown(row.get('description'))} "
            f"({escape_markdown(row.get('occurred_on'))})"
        )
    if len(rows) > max_items:
        lines.append(f"\n...dan {len(rows) - max_items} transaksi lainnya.")
    lines.append("\n🔍 *Pencarian selesai.*")
    return "\n".join(lines)


def format_monthly_stats(
    summary: Mapping[str, Any],
    previous_summary: Mapping[str, Any] | None,
    now: datetime,
) -> str:
    """Render Indonesian monthly expense statistics with trend/projection."""
    expense = summary.get("expense") or {}
    count = int(expense.get("count") or 0)
    if count == 0:
        return "📊 Belum ada pengeluaran untuk bulan ini."
    total = int(expense.get("total_idr") or 0)
    previous_expense = (previous_summary or {}).get("expense") or {}
    last_total = int(previous_expense.get("total_idr") or 0)
    lines = [f"📊 *Ringkasan {_month_name(now.month)} {now.year}*\n", f"💰 Total: Rp {_idr(total)}"]
    if last_total > 0:
        delta_pct = (total - last_total) / last_total * 100
        sign = "+" if delta_pct >= 0 else ""
        lines.append(f"📈 vs bulan lalu: {sign}{delta_pct:.0f}% (Rp {_idr(last_total)})")
    days_elapsed = max(1, now.day)
    next_month = now.replace(year=now.year + 1, month=1, day=1) if now.month == 12 else now.replace(month=now.month + 1, day=1)
    days_in_month = (next_month - timedelta(days=1)).day
    daily_avg = total / days_elapsed
    projected = daily_avg * days_in_month
    lines.append(f"📋 Transaksi: {count}  •  Rata-rata/hari: Rp {daily_avg:,.0f}")
    lines.append(f"🔮 Proyeksi akhir bulan: Rp ~{projected:,.0f}")
    income = summary.get("income") or {}
    if int(income.get("count") or 0):
        lines.append(f"💵 Pemasukan: Rp {_idr(income.get('total_idr'))} ({int(income.get('count') or 0)} transaksi)")
    lines.extend(("", "Top kategori:"))
    categories = sorted((expense.get("by_category") or {}).items(), key=lambda item: -int(item[1] or 0))
    medals = ["🥇", "🥈", "🥉"]
    for index, (category, amount) in enumerate(categories[:3]):
        amount = int(amount or 0)
        lines.append(f"  {medals[index]} {escape_markdown(category)}: Rp {_idr(amount)} ({amount / total * 100:.0f}%)")
    rest = categories[3:]
    if rest:
        rest_total = sum(int(value or 0) for _, value in rest)
        lines.append(f"  • Lainnya: Rp {_idr(rest_total)} ({rest_total / total * 100:.0f}%)")
    biggest = expense.get("biggest")
    if biggest:
        occurred = str(biggest.get("occurred_on") or "")
        try:
            day, month = int(occurred[8:10]), _month_short(int(occurred[5:7]))
            date_label = f"{day} {month}"
        except (ValueError, TypeError):
            date_label = escape_markdown(occurred)
        lines.extend(("", f"💸 Terbesar: Rp {_idr(biggest.get('amount_idr'))} — {escape_markdown(biggest.get('description'))} ({date_label})"))
    return "\n".join(lines)


def format_budget_report(
    rows: list[Mapping[str, Any]], month: str | None = None
) -> str:
    """Render a concise, Markdown-safe monthly budget report.

    ``BudgetStore.report`` is the canonical producer, but the aliases used
    by older callers (``name``, ``budget``, ``spent`` and ``period``) are
    accepted so this presentation helper remains useful at API boundaries.
    """
    if not rows:
        period = month or "bulan ini"
        return f"💰 Belum ada budget untuk {escape_markdown(period)}."

    period = month or str(rows[0].get("month") or rows[0].get("period") or "bulan ini")
    heading = f"🎯 *Budget {escape_markdown(_budget_period_label(period))}*"
    lines = [heading, ""]
    total_budget = total_spent = 0
    for row in rows:
        category = row.get("category") or row.get("name") or "Tanpa kategori"
        budget = int(row.get("budget_idr") or row.get("budget") or 0)
        spent = int(row.get("spent_idr") or row.get("spent") or 0)
        remaining = int(row.get("remaining_idr") if row.get("remaining_idr") is not None else budget - spent)
        percentage = float(row.get("percentage") or (spent / budget * 100 if budget else 0))
        status = str(row.get("status") or ("over" if percentage >= 100 else "warning" if percentage >= 80 else "ok")).lower()
        icon = {"over": "🔴", "warning": "⚠️", "ok": "✅"}.get(status, "•")
        status_label = {"over": "kelewat", "warning": "mendekati batas", "ok": "aman"}.get(status, status)
        lines.append(
            f"{icon} *{escape_markdown(category)}*: "
            f"Rp {_idr(spent)} / Rp {_idr(budget)} ({percentage:.0f}%)"
        )
        lines.append(f"   Sisa: Rp {_idr(remaining)} · {escape_markdown(status_label)}")
        total_budget += budget
        total_spent += spent
    lines.extend(("", f"Total: Rp {_idr(total_spent)} / Rp {_idr(total_budget)}"))
    return "\n".join(lines)


def _budget_period_label(period: str) -> str:
    """Turn YYYY-MM into an Indonesian month label; preserve other values."""
    try:
        year, month = period.split("-", 1)
        month_number = int(month)
        if len(year) == 4 and 1 <= month_number <= 12:
            return f"{_month_name(month_number)} {year}"
    except (AttributeError, ValueError):
        pass
    return str(period)


def _month_name(month: int) -> str:
    return ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober", "November", "Desember"][month]


def _month_short(month: int) -> str:
    return ["", "Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"][month]
