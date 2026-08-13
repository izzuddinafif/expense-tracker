from datetime import date

import pytest

from budget_commands import BudgetCommandService


class FakeStore:
    def __init__(self):
        self.calls = []
        self.rows = [{"month": "2026-07", "category": "Makan", "budget_idr": 100000, "spent_idr": 50000, "percentage": 50, "remaining_idr": 50000, "status": "ok"}]

    async def report(self, user_id, month):
        self.calls.append(("report", user_id, month))
        return [row for row in self.rows if row["month"] == month]

    async def set(self, user_id, month, category, amount):
        self.calls.append(("set", user_id, month, category, amount))

    async def delete(self, user_id, month, category):
        self.calls.append(("delete", user_id, month, category))
        return category == "Makan"


def parser(value):
    return float(value.replace(".", ""))


@pytest.fixture
def service():
    store = FakeStore()
    return BudgetCommandService(store, parser), store


@pytest.mark.asyncio
async def test_report_current_and_explicit_month_markdown(service):
    svc, store = service
    result = await svc.execute(7, "/budget@ledgerly_bot", date(2026, 7, 29))
    assert result.parse_mode == "Markdown"
    assert "Budget Juli 2026" in result.text
    result = await svc.execute(7, "/budget 2026-07")
    assert store.calls[-1] == ("report", 7, "2026-07")


@pytest.mark.asyncio
async def test_set_current_and_explicit_month_enforces_integer(service):
    svc, store = service
    await svc.execute(7, "/budget set 1.500 Makan", date(2026, 7, 1))
    assert store.calls[-1] == ("set", 7, "2026-07", "Makan", 1500)
    await svc.execute(7, "/budget set 2026-08 250000 Transport")
    assert store.calls[-1] == ("set", 7, "2026-08", "Transport", 250000)
    with pytest.raises(ValueError, match="Jumlah tidak valid"):
        await svc.execute(7, "/budget set 1,5 Makan")


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_supports_month(service):
    svc, store = service
    assert "Dihapus" in (await svc.execute(7, "/budget delete Makan", date(2026, 7, 1))).text
    assert "Tidak ditemukan" in (await svc.execute(7, "/budget delete 2026-08 Transport")).text
    assert store.calls[-1] == ("delete", 7, "2026-08", "Transport")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["/budget 2026-7", "/budget 2026-13", "/budget set 2026-7 10 Food", "/budget set 10", "/budget delete"])
async def test_invalid_usage(raw, service):
    svc, _ = service
    with pytest.raises(ValueError):
        await svc.execute(7, raw)


@pytest.mark.asyncio
async def test_extra_report_args_rejected(service):
    svc, _ = service
    with pytest.raises(ValueError, match="Gunakan /budget"):
        await svc.execute(7, "/budget July now")
