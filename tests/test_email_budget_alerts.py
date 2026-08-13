from types import SimpleNamespace

import pytest

from email_watcher import EmailWatcher
from models import ExpenseEntry


@pytest.mark.asyncio
async def test_budget_alert_uses_user_scoped_local_reporter():
    calls = []
    alerts = []

    async def report(user_id: int, month: str):
        calls.append((user_id, month))
        return [
            {
                "name": "Warung",
                "budget": 100_000,
                "spent": 85_000,
                "percentage": 85,
                "subcategories": ["Warung"],
            }
        ]

    async def alert(text: str):
        alerts.append(text)

    watcher = EmailWatcher(
        config=SimpleNamespace(),
        db=object(),
        notion=None,
        agent=None,
        cache_getter=lambda: None,
        alert_fn=alert,
        budget_reporter=report,
    )
    entry = ExpenseEntry(
        description="Nasi goreng",
        amount=25_000,
        date="2026-07-20",
        subcategory="Warung/Makan Siap Saji",
        account="Cash",
        confidence=0.9,
    )

    await watcher._check_budget_alert(entry, user_id=7)

    assert calls == [(7, "2026-07")]
    assert len(alerts) == 1
    assert "hampir habis" in alerts[0]


@pytest.mark.asyncio
async def test_budget_alert_ignores_unrelated_local_budget():
    async def report(_user_id: int, _month: str):
        return [
            {
                "name": "Transport",
                "budget": 100_000,
                "spent": 120_000,
                "percentage": 120,
                "subcategories": ["Transport"],
            }
        ]

    alerts = []
    watcher = EmailWatcher(
        config=SimpleNamespace(),
        db=object(),
        notion=None,
        agent=None,
        cache_getter=lambda: None,
        alert_fn=alerts.append,
        budget_reporter=report,
    )
    entry = ExpenseEntry(
        description="Nasi goreng",
        amount=25_000,
        date="2026-07-20",
        subcategory="Warung",
        account="Cash",
        confidence=0.9,
    )

    await watcher._check_budget_alert(entry, user_id=7)

    assert alerts == []
