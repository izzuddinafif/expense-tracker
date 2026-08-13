"""Focused API contracts for manual transaction provenance."""

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import register_api_routes
from db import Database


@pytest_asyncio.fixture
async def api_client(tmp_path):
    db = await Database.connect(str(tmp_path / "manual-source.db"))
    app = web.Application()
    register_api_routes(app, db=db, token="test-device-token", user_id=7)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()
        await db.close()


def _headers(ref: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer test-device-token",
        "Idempotency-Key": ref,
    }


def _payload(**overrides):
    payload = {
        "kind": "expense",
        "amount_idr": 25_000,
        "occurred_on": "2026-07-29",
        "description": "Cash purchase",
        "merchant": "Warung",
        "account": "Jago",
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_manual_source_persists_and_replay_is_idempotent(api_client):
    client, db = api_client
    headers = _headers("manual:cash-1")
    first = await client.post(
        "/api/v1/transactions",
        headers=headers,
        json=_payload(source="manual"),
    )
    assert first.status == 201
    first_row = (await first.json())["transaction"]
    assert first_row["source"] == "manual"
    assert first_row["status"] == "pending"

    replay = await client.post(
        "/api/v1/transactions",
        headers=headers,
        json=_payload(source="manual", description="Changed on replay"),
    )
    assert replay.status == 200
    replay_row = (await replay.json())["transaction"]
    assert replay_row["id"] == first_row["id"]
    assert replay_row["description"] == "Cash purchase"
    count = await (
        await db._conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE user_id=?", (7,)
        )
    ).fetchone()
    assert count[0] == 1


@pytest.mark.asyncio
async def test_later_matching_notification_is_retained_without_shared_bank_reference(api_client):
    client, db = api_client
    manual = await client.post(
        "/api/v1/transactions",
        headers=_headers("manual:cash-2"),
        json=_payload(source="manual"),
    )
    notification = await client.post(
        "/api/v1/transactions",
        headers=_headers("jago:notif-2"),
        json=_payload(source="android_notification"),
    )
    assert manual.status == 201
    assert notification.status == 201
    manual_row = (await manual.json())["transaction"]
    notification_row = (await notification.json())["transaction"]
    assert manual_row["source"] == "manual"
    assert notification_row["source"] == "android_notification"
    assert notification_row["id"] != manual_row["id"]
    count = await (
        await db._conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE user_id=?", (7,)
        )
    ).fetchone()
    assert count[0] == 2


@pytest.mark.asyncio
async def test_source_defaults_to_notification_and_rejects_untrusted_value(api_client):
    client, _ = api_client
    defaulted = await client.post(
        "/api/v1/transactions",
        headers=_headers("jago:notif-default"),
        json=_payload(),
    )
    assert defaulted.status == 201
    assert (await defaulted.json())["transaction"]["source"] == "android_notification"

    invalid = await client.post(
        "/api/v1/transactions",
        headers=_headers("manual:invalid-source"),
        json=_payload(source="imported_csv"),
    )
    assert invalid.status == 400
    assert (await invalid.json())["error"] == "Invalid transaction source"
