import asyncio

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api import register_api_routes
from db import Database
from email_watcher import _extract_self_transfer_evidence
from local_budgets import BudgetStore
from reporting import LedgerReporting


REFERENCE = "TRX-JAGO-ABC12345"


async def email_bundle(
    db: Database,
    *,
    amount: int = 500_000,
    uid: str = "77",
    with_evidence: bool = True,
):
    return await db.create_confirmed_self_transfer(
        7,
        amount_idr=amount,
        admin_fee_idr=2_500,
        occurred_on="2026-08-14",
        outgoing_description="Transfer antar rekening — Mandiri → Jago (keluar)",
        incoming_description="Transfer antar rekening — Mandiri → Jago (masuk)",
        fee_description="Biaya admin transfer — Mandiri → Jago",
        outgoing_subcategory="Transfer",
        incoming_subcategory="Transfer",
        source_account="Mandiri",
        destination_account="Jago",
        email_uid=uid,
        sender="noreply.livin@bankmandiri.co.id",
        evidence_scheme="bank_reference" if with_evidence else None,
        evidence_reference=REFERENCE if with_evidence else None,
    )


async def android_capture(
    db: Database,
    *,
    amount: int = 500_000,
    source_ref: str = "jago:notification-1",
    kind: str = "income",
    account: str = "Jago",
):
    return await db.ingest_android_self_transfer(
        7,
        kind=kind,
        amount_idr=amount,
        occurred_on="2026-08-14",
        description="Dana masuk" if kind == "income" else "Transfer keluar",
        merchant="Transfer masuk",
        category="Transfer",
        subcategory="Transfer",
        account=account,
        source_ref=source_ref,
        evidence_scheme="bank_reference",
        evidence_reference=REFERENCE,
        metadata={"package_name": "com.jago.digitalBanking"},
    )


@pytest.mark.asyncio
async def test_email_first_reuses_canonical_income_and_never_stores_raw_reference(tmp_path):
    db = await Database.connect(str(tmp_path / "email-first.db"))
    try:
        bundle = await email_bundle(db)
        outcome = await android_capture(db)
        replay = await android_capture(db)

        assert outcome.code == replay.code == "reused_canonical_transfer"
        assert outcome.action == "finalize"
        assert outcome.transaction["id"] == bundle["incoming"]["id"]
        assert replay.transaction["id"] == bundle["incoming"]["id"]
        count = await (await db._conn.execute("SELECT COUNT(*) FROM transactions")).fetchone()
        assert count[0] == 3
        dump = " ".join(
            row[0] or ""
            for row in await (
                await db._conn.execute(
                    "SELECT evidence_key FROM self_transfer_correlations "
                    "UNION ALL SELECT metadata_json FROM transaction_events"
                )
            ).fetchall()
        )
        assert REFERENCE not in dump
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_android_first_is_pending_then_promoted_to_canonical_income(tmp_path):
    db = await Database.connect(str(tmp_path / "android-first.db"))
    try:
        capture = await android_capture(db)
        assert capture.created is True
        assert capture.code == "awaiting_canonical_email"
        assert capture.action == "keep_review"
        assert capture.transaction["status"] == "pending"

        bundle = await email_bundle(db)
        assert bundle["incoming"]["id"] == capture.transaction["id"]
        assert bundle["incoming"]["status"] == "confirmed"
        assert bundle["incoming"]["source"] == "android_notification"

        replay = await android_capture(db)
        assert replay.code == "reused_canonical_transfer"
        assert replay.action == "finalize"
        count = await (await db._conn.execute("SELECT COUNT(*) FROM transactions")).fetchone()
        outbox = await (await db._conn.execute("SELECT COUNT(*) FROM sync_outbox")).fetchone()
        assert count[0] == 3
        # Transfer principal is authoritative in the ledger but is not sent
        # to either Notion expense/income database; only the admin fee syncs.
        assert outbox[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_transfer_principal_is_not_spending_or_income(tmp_path):
    db = await Database.connect(str(tmp_path / "transfer-accounting.db"))
    try:
        bundle = await email_bundle(db, amount=5_000_000)
        summary = await LedgerReporting(db).monthly_summary(7, "2026-08")
        assert summary["expense"]["total_idr"] == 2_500
        assert summary["income"]["total_idr"] == 0
        assert summary["transfer_count"] == 1
        context = await LedgerReporting(db).expense_context(7)
        assert [row["amount"] for row in context] == [2_500]

        budgets = BudgetStore(db)
        await budgets.set(7, "2026-08", "Transfer", 10_000)
        report = await budgets.report(7, "2026-08")
        assert report[0]["spent_idr"] == 2_500
        assert bundle["outgoing"]["ledger_role"] == "self_transfer_principal"
        assert bundle["incoming"]["ledger_role"] == "self_transfer_principal"
        assert bundle["fee"]["ledger_role"] == "self_transfer_fee"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_transfer_principal_legs_reject_independent_mutations(tmp_path):
    db = await Database.connect(str(tmp_path / "transfer-mutations.db"))
    try:
        bundle = await email_bundle(db)
        outgoing = bundle["outgoing"]

        with pytest.raises(ValueError, match="cannot be edited independently"):
            await db.update_transaction(
                7,
                outgoing["id"],
                {"description": "Tampered transfer"},
                expected_updated_at=outgoing["updated_at"],
            )
        with pytest.raises(ValueError, match="cannot be voided independently"):
            await db.void_transaction(
                7,
                outgoing["id"],
                expected_updated_at=outgoing["updated_at"],
            )

        refreshed = await db.find_transaction_by_id(7, outgoing["id"])
        assert refreshed["status"] == "confirmed"
        assert refreshed["description"] == outgoing["description"]
        events = await (
            await db._conn.execute(
                "SELECT event_type FROM transaction_events WHERE transaction_id=?",
                (outgoing["id"],),
            )
        ).fetchall()
        assert {event["event_type"] for event in events} == {"confirmed_external"}
        principal_jobs = await (
            await db._conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE transaction_id IN (?, ?)",
                (bundle["outgoing"]["id"], bundle["incoming"]["id"]),
            )
        ).fetchone()
        assert principal_jobs[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_confirming_a_transfer_principal_never_queues_notion(tmp_path):
    db = await Database.connect(str(tmp_path / "transfer-confirm.db"))
    try:
        capture = await db.ingest_android_self_transfer(
            7,
            kind="expense",
            amount_idr=500_000,
            occurred_on="2026-08-14",
            description="Transfer keluar",
            account="Mandiri",
            source_ref="mandiri:unmatched-transfer",
        )
        row, changed = await db.confirm_transaction(7, capture.transaction["id"])
        assert changed is True
        assert row["status"] == "confirmed"
        jobs = await (
            await db._conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE transaction_id=?",
                (row["id"],),
            )
        ).fetchone()
        assert jobs[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_android_first_outgoing_is_promoted_without_duplicate_principal(tmp_path):
    db = await Database.connect(str(tmp_path / "android-outgoing.db"))
    try:
        capture = await android_capture(
            db,
            kind="expense",
            account="Mandiri",
            source_ref="mandiri:notification-outgoing",
        )
        assert capture.transaction["status"] == "pending"

        bundle = await email_bundle(db, uid="outgoing-no-evidence", with_evidence=False)
        assert bundle["outgoing"]["id"] == capture.transaction["id"]
        assert bundle["outgoing"]["source"] == "android_notification"
        assert bundle["outgoing"]["ledger_role"] == "self_transfer_principal"
        count = await (
            await db._conn.execute(
                "SELECT COUNT(*) FROM transactions WHERE kind='expense' "
                "AND ledger_role='self_transfer_principal'"
            )
        ).fetchone()
        assert count[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_arrival_and_replay_create_one_incoming_leg(tmp_path):
    db = await Database.connect(str(tmp_path / "concurrent.db"))
    try:
        bundle_result, capture_result = await asyncio.gather(
            email_bundle(db),
            android_capture(db),
        )
        canonical_id = bundle_result["incoming"]["id"]
        assert capture_result.transaction["id"] == canonical_id
        replay = await android_capture(db, source_ref="jago:notification-repost")
        assert replay.transaction["id"] == canonical_id
        assert replay.code == "reused_canonical_transfer"
        incomes = await (
            await db._conn.execute("SELECT id FROM transactions WHERE kind='income'")
        ).fetchall()
        assert [row["id"] for row in incomes] == [canonical_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_conflicting_evidence_keeps_capture_in_review_and_email_is_canonical(tmp_path):
    db = await Database.connect(str(tmp_path / "conflict.db"))
    try:
        capture = await android_capture(db, amount=499_000)
        bundle = await email_bundle(db, amount=500_000)
        retry = await android_capture(db, amount=499_000)

        assert capture.transaction["status"] == "pending"
        assert bundle["incoming"]["id"] != capture.transaction["id"]
        assert retry.code == "evidence_conflict"
        assert retry.action == "keep_review"
        assert retry.transaction["status"] == "confirmed"
        incomes = await (
            await db._conn.execute(
                "SELECT status,amount_idr FROM transactions WHERE kind='income' ORDER BY amount_idr"
            )
        ).fetchall()
        assert [(row["status"], row["amount_idr"]) for row in incomes] == [
            ("pending", 499_000),
            ("confirmed", 500_000),
        ]
    finally:
        await db.close()


@pytest_asyncio.fixture
async def api_client(tmp_path):
    db = await Database.connect(str(tmp_path / "self-transfer-api.db"))
    app = web.Application()
    register_api_routes(app, db=db, token="token", user_id=7)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, db
    finally:
        await client.close()
        await db.close()


def api_payload(**overrides):
    payload = {
        "kind": "income",
        "amount_idr": 500_000,
        "occurred_on": "2026-08-14",
        "description": "Dana masuk",
        "merchant": "Transfer masuk",
        "category": "Transfer",
        "subcategory": "Transfer",
        "account": "JAGO",
        "source": "android_notification",
        "self_transfer": True,
        "transfer_evidence": {
            "scheme": "bank_reference",
            "reference": REFERENCE,
        },
        "confirm": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_api_typed_outcomes_and_external_income_safety(api_client):
    client, db = api_client
    headers = {"Authorization": "Bearer token", "Idempotency-Key": "jago:1"}

    waiting = await client.post("/api/v1/transactions", headers=headers, json=api_payload())
    assert waiting.status == 202
    waiting_body = await waiting.json()
    assert waiting_body["transaction"]["status"] == "pending"
    assert waiting_body["ingestion_outcome"] == {
        "code": "awaiting_canonical_email",
        "action": "keep_review",
    }

    bundle = await email_bundle(db)
    reused = await client.post("/api/v1/transactions", headers=headers, json=api_payload())
    assert reused.status == 200
    reused_body = await reused.json()
    assert reused_body["transaction"]["id"] == bundle["incoming"]["id"]
    assert reused_body["ingestion_outcome"] == {
        "code": "reused_canonical_transfer",
        "action": "finalize",
    }

    no_evidence = await client.post(
        "/api/v1/transactions",
        headers={"Authorization": "Bearer token", "Idempotency-Key": "jago:2"},
        json=api_payload(transfer_evidence=None),
    )
    assert no_evidence.status == 200
    assert (await no_evidence.json())["ingestion_outcome"] == {
        "code": "reused_canonical_transfer",
        "action": "finalize",
    }

    external = await client.post(
        "/api/v1/transactions",
        headers={"Authorization": "Bearer token", "Idempotency-Key": "jago:salary"},
        json=api_payload(
            self_transfer=False,
            transfer_evidence=None,
            description="Salary",
            merchant="Employer",
        ),
    )
    assert external.status == 201
    assert (await external.json())["transaction"]["id"] != bundle["incoming"]["id"]


def test_email_evidence_requires_one_explicit_label():
    assert _extract_self_transfer_evidence(
        "Transfer berhasil", f"Nomor Referensi: {REFERENCE}"
    ) == ("bank_reference", REFERENCE)
    assert _extract_self_transfer_evidence(
        "Transfer berhasil", "Rp500.000 pada 14/08/2026"
    ) is None
    assert _extract_self_transfer_evidence(
        "Transfer berhasil",
        "Nomor Referensi: ABC123456\nTransaction ID: XYZ987654",
    ) is None
