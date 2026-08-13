from datetime import date, timedelta

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import register_api_routes
from db import Database
from local_budgets import BudgetStore


HEADERS = {"Authorization": "Bearer budget-api-token"}


@pytest_asyncio.fixture
async def budget_api_client(tmp_path):
    db = await Database.connect(str(tmp_path / "budget-api.db"))
    app = web.Application()
    register_api_routes(app, db=db, token="budget-api-token", user_id=7)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()
        await db.close()


async def _add_transaction(
    db: Database,
    *,
    user_id: int,
    source_ref: str,
    amount_idr: int,
    occurred_on: str = "2026-07-12",
    kind: str = "expense",
    confirmed: bool = True,
) -> None:
    row, _ = await db.create_ingested_transaction(
        user_id,
        kind=kind,
        amount_idr=amount_idr,
        occurred_on=occurred_on,
        description="Budget test",
        category="Food",
        subcategory="Dining",
        account="Jago",
        source_ref=source_ref,
    )
    if confirmed:
        await db.confirm_transaction(user_id, row["id"])


@pytest.mark.asyncio
async def test_budget_endpoints_require_authentication(budget_api_client):
    client, _ = budget_api_client
    assert (await client.get("/api/v1/budgets")).status == 401
    assert (
        await client.put(
            "/api/v1/budgets",
            json={"month": "2026-07", "category": "Dining", "amount_idr": 1},
        )
    ).status == 401
    assert (await client.delete("/api/v1/budgets?category=Dining")).status == 401


@pytest.mark.asyncio
async def test_budget_crud_reports_only_confirmed_user_expenses(budget_api_client):
    client, db = budget_api_client
    await _add_transaction(db, user_id=7, source_ref="budget:confirmed", amount_idr=85_000)
    await _add_transaction(
        db, user_id=7, source_ref="budget:pending", amount_idr=90_000, confirmed=False
    )
    await _add_transaction(
        db, user_id=7, source_ref="budget:income", amount_idr=90_000, kind="income"
    )
    await _add_transaction(db, user_id=8, source_ref="budget:other-user", amount_idr=90_000)

    created = await client.put(
        "/api/v1/budgets",
        headers=HEADERS,
        json={"month": "2026-07", "category": "Dining", "amount_idr": 100_000},
    )
    assert created.status == 200
    body = await created.json()
    assert body == {
        "month": "2026-07",
        "budgets": [
            {
                "month": "2026-07",
                "category": "Dining",
                "amount_idr": 100_000,
                "spent_idr": 85_000,
                "remaining_idr": 15_000,
                "percentage": 85,
                "status": "warning",
            }
        ],
    }

    listing = await client.get("/api/v1/budgets?month=2026-07", headers=HEADERS)
    assert listing.status == 200
    assert await listing.json() == body

    updated = await client.put(
        "/api/v1/budgets",
        headers=HEADERS,
        json={"month": "2026-07", "category": "dining", "amount_idr": 80_000},
    )
    assert (await updated.json())["budgets"][0]["status"] == "over"

    deleted = await client.delete(
        "/api/v1/budgets?month=2026-07&category=Dining", headers=HEADERS
    )
    assert await deleted.json() == {
        "month": "2026-07",
        "category": "Dining",
        "deleted": True,
        "budgets": [],
    }
    repeated = await client.delete(
        "/api/v1/budgets?month=2026-07&category=Dining", headers=HEADERS
    )
    assert (await repeated.json())["deleted"] is False


@pytest.mark.asyncio
async def test_budget_api_validates_payloads_and_month_query(budget_api_client):
    client, _ = budget_api_client
    assert (
        await client.get("/api/v1/budgets?month=2026-7", headers=HEADERS)
    ).status == 400
    assert (
        await client.put(
            "/api/v1/budgets",
            headers=HEADERS,
            json={"month": "2026-07", "category": "Dining", "amount_idr": True},
        )
    ).status == 400
    assert (
        await client.put(
            "/api/v1/budgets",
            headers=HEADERS,
            json={"month": "2026-07", "category": "", "amount_idr": 1},
        )
    ).status == 400
    assert (
        await client.delete("/api/v1/budgets?month=2026-07", headers=HEADERS)
    ).status == 400


@pytest.mark.asyncio
async def test_budget_api_defaults_month_and_never_exposes_other_users(budget_api_client):
    client, db = budget_api_client
    store = BudgetStore(db)
    current_month = date.today().strftime("%Y-%m")
    next_month = (date.today().replace(day=28) + timedelta(days=7)).strftime("%Y-%m")
    await store.set(7, current_month, "Transport", 200_000)
    await store.set(8, current_month, "Private", 999_000)
    await store.set(7, next_month, "Future", 100_000)

    default_response = await client.get("/api/v1/budgets", headers=HEADERS)
    assert default_response.status == 200
    assert await default_response.json() == {
        "month": current_month,
        "budgets": [
            {
                "month": current_month,
                "category": "Transport",
                "amount_idr": 200_000,
                "spent_idr": 0,
                "remaining_idr": 200_000,
                "percentage": 0,
                "status": "ok",
            }
        ],
    }
    future_response = await client.get(
        f"/api/v1/budgets?month={next_month}", headers=HEADERS
    )
    assert (await future_response.json())["budgets"][0]["category"] == "Future"
