import pytest

from db import Database


@pytest.mark.asyncio
async def test_operational_state_preserves_last_success_across_failure(tmp_path):
    db = await Database.connect(str(tmp_path / "health.db"))
    try:
        await db.record_operational_state(
            "gmail",
            success=True,
            metadata={"messages": 2},
        )
        first = (await db.get_operational_health(7))["workers"]["gmail"]
        await db.record_operational_state(
            "gmail",
            success=False,
            error="imap timeout",
        )
        failed = (await db.get_operational_health(7))["workers"]["gmail"]
        assert failed["last_success_at"] == first["last_success_at"]
        assert failed["last_attempt_at"] >= first["last_attempt_at"]
        assert failed["last_error"] == "imap timeout"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_operational_health_includes_scoped_outbox_summary(tmp_path):
    db = await Database.connect(str(tmp_path / "health.db"))
    try:
        row, _ = await db.create_confirmed_external_transaction(
            7,
            kind="expense",
            amount_idr=10_000,
            occurred_on="2026-07-29",
            description="Health test",
            source="manual",
            source_ref="manual:health",
        )
        outbox = await (
            await db._conn.execute(
                "SELECT id FROM sync_outbox WHERE transaction_id=?", (row["id"],)
            )
        ).fetchone()
        await db.mark_notion_sync_failure(outbox["id"], "offline")
        health = await db.get_operational_health(7)
        assert health["status"] == "degraded"
        assert health["outbox"]["depth"] == 1
        assert health["outbox"]["failed"] == 1
    finally:
        await db.close()
