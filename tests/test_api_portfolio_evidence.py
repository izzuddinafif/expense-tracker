import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import _default_portfolio, register_api_routes
from db import Database
from models import UserRecord


@pytest_asyncio.fixture
async def client_and_db(tmp_path):
    db = await Database.connect(str(tmp_path / "portfolio-api.db"))
    app = web.Application()
    register_api_routes(app, db=db, token="portfolio-token", user_id=7)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_portfolio_uses_live_notion_rows_and_valued_local_entries(monkeypatch, tmp_path):
    db = await Database.connect(str(tmp_path / "portfolio.db"))

    class FakeNotion:
        async def fetch_accounts(self):
            return [
                {"title": "Mandiri 1854", "type": "Bank", "initial_amount_idr": 100_000,
                 "current_balance_idr": 125_000, "total_income_idr": 50_000, "total_expenses_idr": 25_000},
                {"title": "Jago", "type": "Bank", "initial_amount_idr": 0,
                 "current_balance_idr": 75_000, "total_income_idr": 75_000, "total_expenses_idr": 0},
            ]

        async def fetch_assets(self):
            return [
                {"name": "Gold", "type": "Commodity", "value_idr": 1_500_000,
                 "last_updated": "2026-08-14", "notes": ""},
                {"name": "Unpriced shares", "type": "Equity", "value_idr": None,
                 "last_updated": "", "notes": "Needs valuation"},
            ]

        async def aclose(self):
            return None

    user = UserRecord(telegram_id=7, owner_name="Afif", notion_token="test", expenses_ds="e",
                      subcategories_ds="s", accounts_ds="a", months_ds="m", years_ds="y",
                      recurring_ds="r", income_ds="i", income_subcategories_ds="s",
                      income_months_ds="m", income_years_ds="y", categories_ds="c", setup_step="done")

    async def fake_user(_user_id):
        return user

    monkeypatch.setattr(db, "get_user", fake_user)
    monkeypatch.setattr("notion.NotionClient.from_user", lambda _user: FakeNotion())
    await db.create_local_asset(7, name="Motorcycle", kind="asset", value_idr=18_000_000, as_of="2026-08-14")
    await db.create_local_asset(7, name="Credit card", kind="liability", value_idr=300_000, as_of="2026-08-14")
    await db.create_local_asset(7, name="Collectible", kind="asset", value_idr=None, as_of="2026-08-14")
    try:
        portfolio = await _default_portfolio(db, 7)
    finally:
        await db.close()

    assert portfolio["total_liquid_idr"] is None
    assert portfolio["total_assets_idr"] == 19_500_000
    assert portfolio["net_worth_idr"] is None
    assert portfolio["freshness"] == "partial"
    assert portfolio["total_liabilities_idr"] == 300_000
    assert {row["name"] for row in portfolio["accounts"]} >= {"Mandiri 1854", "BSI 9400", "Jago", "Cash"}
    assert any("Unpriced shares" in warning for warning in portfolio["warnings"])


@pytest.mark.asyncio
async def test_portfolio_and_local_asset_api_contract(client_and_db):
    client, _db = client_and_db
    headers = {"Authorization": "Bearer portfolio-token"}
    assert (await client.get("/api/v1/portfolio")).status == 401

    created = await client.post("/api/v1/assets", headers=headers, json={
        "name": "Emergency fund", "is_liability": False, "type": "Cash", "value_idr": 500_000,
        "quantity": 1, "unit": "fund", "last_updated": "2026-08-14",
    })
    assert created.status == 201
    asset = (await created.json())["asset"]
    assert asset["name"] == "Emergency fund"
    assert asset["kind"] == "asset"
    assert asset["type"] == "Cash"
    assert asset["value_idr"] == 500_000
    assert asset["quantity"] == 1
    assert asset["unit"] == "fund"
    assert asset["last_updated"] == "2026-08-14"
    assert asset["is_liability"] is False
    changed = await client.patch(f"/api/v1/assets/{asset['id']}", headers=headers, json={"value_idr": 550_000})
    assert (await changed.json())["asset"]["value_idr"] == 550_000
    listed = await client.get("/api/v1/assets", headers=headers)
    assert (await listed.json())["assets"][0]["id"] == asset["id"]
    deleted = await client.delete(f"/api/v1/assets/{asset['id']}", headers=headers)
    assert await deleted.json() == {"deleted": True}


@pytest.mark.asyncio
async def test_exact_bank_reference_merges_evidence_only(client_and_db):
    client, db = client_and_db
    headers = {"Authorization": "Bearer portfolio-token", "Idempotency-Key": "android:proof-1"}
    email, created = await db.create_confirmed_external_transaction(
        7, kind="expense", amount_idr=48_500, occurred_on="2026-08-14", description="QRIS",
        merchant="Warung", account="Mandiri 1854", source="bank_email", source_ref="gmail:1:expense",
        bank_reference="MB-2026-ABC12345",
    )
    assert created is True
    replay = await client.post("/api/v1/transactions", headers=headers, json={
        "kind": "expense", "amount_idr": 48_500, "occurred_on": "2026-08-14",
        "description": "Bank notification", "account": "Mandiri 1854",
        "transfer_evidence": {"scheme": "bank_reference", "reference": "mb 2026 abc12345"},
    })
    body = await replay.json()
    assert replay.status == 200 and body["created"] is False
    assert body["transaction"]["id"] == email["id"]
    fetched = await client.get(f"/api/v1/transactions/{email['id']}", headers={"Authorization": "Bearer portfolio-token"})
    public = (await fetched.json())["transaction"]
    assert public["evidence_count"] == 2
    assert {item["source"] for item in public["evidence"]} == {"bank_email", "android_notification"}
    assert "MB-2026-ABC12345" not in str(public)


@pytest.mark.asyncio
async def test_missing_or_different_bank_references_do_not_merge(tmp_path):
    db = await Database.connect(str(tmp_path / "strict-evidence.db"))
    try:
        first, _ = await db.create_confirmed_external_transaction(
            7, kind="expense", amount_idr=10_000, occurred_on="2026-08-14", description="Lunch",
            account="Cash", source="bank_email", source_ref="gmail:no-ref",
        )
        second, created = await db.create_ingested_transaction(
            7, kind="expense", amount_idr=10_000, occurred_on="2026-08-14", description="Lunch",
            account="Cash", source_ref="android:no-ref",
        )
        third, _ = await db.create_ingested_transaction(
            7, kind="expense", amount_idr=10_000, occurred_on="2026-08-14", description="Lunch",
            account="Cash", source_ref="android:other-ref", bank_reference="OTHER-123456",
        )
        assert created is True
        assert len({first["id"], second["id"], third["id"]}) == 3
    finally:
        await db.close()
