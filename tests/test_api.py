import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import register_api_routes, register_system_routes
from db import Database


@pytest_asyncio.fixture
async def api_client(tmp_path):
    db = await Database.connect(str(tmp_path / "api.db"))
    app = web.Application()
    register_system_routes(app, db=db)
    register_api_routes(app, db=db, token="test-device-token", user_id=7, max_body_bytes=512)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_api_requires_bearer_token(api_client):
    client, _ = api_client
    response = await client.get("/api/v1/health")
    assert response.status == 401


@pytest.mark.asyncio
async def test_liveness_is_non_disclosing_and_unauthenticated(api_client):
    client, _ = api_client
    response = await client.get("/livez")
    assert response.status == 200
    assert await response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_ingestion_is_idempotent_and_confirmable(api_client):
    client, db = api_client
    headers = {
        "Authorization": "Bearer test-device-token",
        "Idempotency-Key": "livin:abc123",
    }
    payload = {
        "kind": "expense",
        "amount_idr": 48_500,
        "occurred_on": "2026-07-28",
        "description": "QRIS purchase",
        "merchant": "Warung Contoh",
        "account": "Mandiri",
    }
    first = await client.post("/api/v1/transactions", headers=headers, json=payload)
    assert first.status == 201
    first_body = await first.json()
    tx_id = first_body["transaction"]["id"]
    assert first_body["transaction"]["status"] == "pending"

    replay = await client.post("/api/v1/transactions", headers=headers, json=payload)
    assert replay.status == 200
    assert (await replay.json())["transaction"]["id"] == tx_id
    count = await (
        await db._conn.execute("SELECT COUNT(*) FROM transactions")
    ).fetchone()
    assert count[0] == 1

    confirmed = await client.patch(
        f"/api/v1/transactions/{tx_id}/confirm",
        headers={"Authorization": "Bearer test-device-token"},
    )
    assert confirmed.status == 200
    assert (await confirmed.json())["transaction"]["status"] == "confirmed"
    outbox = await (
        await db._conn.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE transaction_id=?", (tx_id,)
        )
    ).fetchone()
    assert outbox[0] == 1

    listing = await client.get(
        "/api/v1/transactions",
        headers={"Authorization": "Bearer test-device-token"},
    )
    assert listing.status == 200
    assert [row["id"] for row in (await listing.json())["transactions"]] == [tx_id]


@pytest.mark.asyncio
async def test_api_rejects_oversized_json(api_client):
    client, _ = api_client
    response = await client.post(
        "/api/v1/transactions",
        headers={"Authorization": "Bearer test-device-token"},
        data=b'{"description":"' + (b"x" * 600) + b'"}',
    )
    assert response.status == 413


@pytest.mark.asyncio
async def test_sync_status_and_retry_are_authenticated_and_scoped(api_client):
    client, db = api_client
    row, _ = await db.create_ingested_transaction(
        7,
        kind="expense",
        amount_idr=12_000,
        occurred_on="2026-07-29",
        description="BSI purchase",
        account="BSI",
        source_ref="bsi:status-test",
    )
    await db.confirm_transaction(7, row["id"])
    outbox = await (
        await db._conn.execute(
            "SELECT id FROM sync_outbox WHERE transaction_id=?", (row["id"],)
        )
    ).fetchone()
    await db.mark_notion_sync_failure(
        outbox["id"], "temporary outage", "2099-01-01T00:00:00+00:00"
    )
    headers = {"Authorization": "Bearer test-device-token"}

    denied = await client.get("/api/v1/sync")
    assert denied.status == 401
    status = await client.get("/api/v1/sync", headers=headers)
    body = await status.json()
    assert body["pending_count"] == 1
    assert body["failed_count"] == 1
    assert body["recent_errors"][0]["transaction_id"] == row["id"]

    retried = await client.post("/api/v1/sync/retry", headers=headers)
    assert await retried.json() == {"retried": 1}
    updated = await (
        await db._conn.execute(
            "SELECT next_attempt_at FROM sync_outbox WHERE id=?", (outbox["id"],)
        )
    ).fetchone()
    assert updated["next_attempt_at"] is None


@pytest.mark.asyncio
async def test_transaction_can_be_edited_and_voided(api_client):
    client, db = api_client
    row, _ = await db.create_confirmed_external_transaction(
        7,
        kind="expense",
        amount_idr=25_000,
        occurred_on="2026-07-29",
        description="Lunch",
        merchant="Warung",
        source="manual",
        source_ref="manual:edit-test",
    )
    await db.mark_notion_sync_success(row["id"], "notion-page-edit")
    headers = {"Authorization": "Bearer test-device-token"}

    edited = await client.patch(
        f"/api/v1/transactions/{row['id']}",
        headers=headers,
        json={
            "amount_idr": 30_000,
            "merchant": "Warung Baru",
            "occurred_on": "2026-07-28",
        },
    )
    assert edited.status == 200
    edited_body = await edited.json()
    assert edited_body["transaction"]["amount_idr"] == 30_000
    assert edited_body["transaction"]["merchant"] == "Warung Baru"

    invalid = await client.patch(
        f"/api/v1/transactions/{row['id']}",
        headers=headers,
        json={"kind": "income"},
    )
    assert invalid.status == 400

    voided = await client.delete(
        f"/api/v1/transactions/{row['id']}", headers=headers
    )
    assert voided.status == 200
    body = await voided.json()
    assert body["transaction"]["status"] == "voided"
    assert body["changed"] is True
    replay = await client.delete(
        f"/api/v1/transactions/{row['id']}", headers=headers
    )
    assert (await replay.json())["changed"] is False


@pytest.mark.asyncio
async def test_operational_health_endpoint(api_client):
    client, db = api_client
    await db.record_operational_state(
        "gmail", success=True, metadata={"messages_found": 1}
    )
    response = await client.get(
        "/api/v1/ops/health",
        headers={"Authorization": "Bearer test-device-token"},
    )
    assert response.status == 200
    body = await response.json()
    assert body["status"] == "ok"
    assert body["outbox"]["depth"] == 0
    assert body["workers"]["gmail"]["last_success_at"]


@pytest.mark.asyncio
async def test_reconciliation_endpoint_is_authenticated_and_injectable(tmp_path):
    db = await Database.connect(str(tmp_path / "reconcile-api.db"))
    app = web.Application()

    async def reconcile(_db, user_id):
        assert _db is db
        assert user_id == 7
        return {"is_clean": False, "missing_remote": [{"transaction_id": "tx-1"}]}

    register_api_routes(
        app,
        db=db,
        token="test-device-token",
        user_id=7,
        reconciler=reconcile,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/api/v1/reconciliation")).status == 401
        response = await client.get(
            "/api/v1/reconciliation",
            headers={"Authorization": "Bearer test-device-token"},
        )
        assert response.status == 200
        assert (await response.json())["missing_remote"][0]["transaction_id"] == "tx-1"
    finally:
        await client.close()
        await db.close()


@pytest.mark.asyncio
async def test_transaction_change_feed_is_keyset_scoped_and_includes_voids(api_client):
    client, db = api_client
    rows = []
    for index in range(3):
        row, _ = await db.create_confirmed_external_transaction(
            7,
            kind="expense",
            amount_idr=1000 + index,
            occurred_on="2026-07-29",
            description=f"change-{index}",
            source="manual",
            source_ref=f"change:{index}",
        )
        rows.append(row)
    # Force a timestamp tie; UUID remains the deterministic tie-breaker.
    await db._conn.execute(
        "UPDATE transactions SET updated_at=? WHERE user_id=?", ("2026-01-01T00:00:00+00:00", 7)
    )
    await db._conn.commit()
    await db.void_transaction(7, rows[1]["id"])
    await db._conn.execute(
        "UPDATE transactions SET updated_at=? WHERE user_id=?", ("2026-01-01T00:00:00+00:00", 7)
    )
    await db._conn.commit()
    outsider, _ = await db.create_confirmed_external_transaction(
        8,
        kind="expense", amount_idr=999, occurred_on="2026-07-29",
        description="other-user", source="manual", source_ref="other:1",
    )
    headers = {"Authorization": "Bearer test-device-token"}

    first = await client.get("/api/v1/transactions/changes?limit=2", headers=headers)
    assert first.status == 200
    first_body = await first.json()
    assert len(first_body["transactions"]) == 2
    assert [r["id"] for r in first_body["transactions"]] == sorted(
        [r["id"] for r in first_body["transactions"]]
    )
    assert first_body["next_cursor"]
    second = await client.get(
        "/api/v1/transactions/changes?limit=2&cursor=" + first_body["next_cursor"],
        headers=headers,
    )
    assert second.status == 200
    second_body = await second.json()
    all_ids = [r["id"] for r in first_body["transactions"] + second_body["transactions"]]
    assert outsider["id"] not in all_ids
    assert rows[1]["id"] in all_ids
    assert all(r["status"] in {"confirmed", "voided"} for r in first_body["transactions"] + second_body["transactions"])
    assert second_body["next_cursor"] is None
    assert second_body["checkpoint_cursor"]
    checkpoint = await client.get(
        "/api/v1/transactions/changes?cursor=" + second_body["checkpoint_cursor"],
        headers=headers,
    )
    assert checkpoint.status == 200
    assert (await checkpoint.json())["transactions"] == []


@pytest.mark.asyncio
async def test_transaction_change_feed_rejects_invalid_cursor_and_requires_auth(api_client):
    client, _ = api_client
    assert (await client.get("/api/v1/transactions/changes")).status == 401
    headers = {"Authorization": "Bearer test-device-token"}
    invalid = await client.get(
        "/api/v1/transactions/changes?cursor=eyJ1Ijo5OTksInQiOiJ4IiwiaSI6InkifQ.bad",
        headers=headers,
    )
    assert invalid.status == 400


@pytest.mark.asyncio
async def test_email_failures_are_visible_and_retryable(api_client):
    client, db = api_client
    for _ in range(3):
        await db.record_email_processing_failure(
            "99123", "bank@example.test", "parse: model unavailable"
        )
    headers = {"Authorization": "Bearer test-device-token"}

    assert (await client.get("/api/v1/email-failures")).status == 401
    response = await client.get("/api/v1/email-failures", headers=headers)
    assert response.status == 200
    failure = (await response.json())["failures"][0]
    assert failure["uid"] == "99123"
    assert failure["status"] == "degraded"
    assert failure["attempt_count"] == 3

    retried = await client.post(
        "/api/v1/email-failures/99123/retry", headers=headers
    )
    assert retried.status == 200
    assert await retried.json() == {"retried": True, "uid": "99123"}
    assert (await db.get_email_failure_summary())["degraded"] == 0
    assert (
        await client.post("/api/v1/email-failures/99123/retry", headers=headers)
    ).status == 404


@pytest.mark.asyncio
async def test_api_canonicalizes_dates_and_rejects_transfer_before_outbox(api_client):
    client, db = api_client
    headers = {
        "Authorization": "Bearer test-device-token",
        "Idempotency-Key": "canonical:api",
    }
    created = await client.post(
        "/api/v1/transactions",
        headers=headers,
        json={
            "kind": "expense",
            "amount_idr": 1,
            "occurred_on": "20260729",
            "description": "canonical",
        },
    )
    assert created.status == 201
    transaction = (await created.json())["transaction"]
    assert transaction["occurred_on"] == "2026-07-29"

    transfer = await client.post(
        "/api/v1/transactions",
        headers={
            "Authorization": "Bearer test-device-token",
            "Idempotency-Key": "transfer:api",
        },
        json={
            "kind": "transfer",
            "amount_idr": 1,
            "occurred_on": "2026-07-29",
            "description": "move",
        },
    )
    assert transfer.status == 400
    assert "Transfer transactions" in (await transfer.json())["error"]
    count = await (
        await db._conn.execute("SELECT COUNT(*) FROM sync_outbox")
    ).fetchone()
    assert count[0] == 0
