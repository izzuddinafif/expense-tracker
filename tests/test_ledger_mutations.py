import pytest

from db import Database, TransactionConflictError


async def make_db(tmp_path):
    return await Database.connect(str(tmp_path / "mutations.db"))


async def make_transaction(db):
    row, _ = await db.create_confirmed_external_transaction(
        7,
        kind="expense",
        amount_idr=12000,
        occurred_on="2026-07-29",
        description="Lunch",
        source="manual",
        source_ref="manual:1",
        account="Cash",
    )
    return row["id"]


@pytest.mark.asyncio
async def test_update_transaction_is_audited_and_reopens_upsert(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        await db.mark_notion_sync_success(tx_id, "page-1")
        row, changed = await db.update_transaction(7, tx_id, {"description": "Dinner"})
        assert changed is True
        assert row["description"] == "Dinner"
        row, changed = await db.update_transaction(7, tx_id, {"description": "Dinner"})
        assert changed is False
        assert (await (await db._conn.execute(
            "SELECT COUNT(*) FROM transaction_events WHERE transaction_id=? AND event_type='edited'", (tx_id,)
        )).fetchone())[0] == 1
        job = await (await db._conn.execute(
            "SELECT operation,completed_at FROM sync_outbox WHERE transaction_id=? ORDER BY id DESC LIMIT 1", (tx_id,)
        )).fetchone()
        assert job["operation"] == "upsert" and job["completed_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edit_revisions_failed_upsert_and_resolves_page_id(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        await db.mark_notion_sync_success(tx_id, "abcd-1234")
        row, _ = await db.update_transaction(7, tx_id, {"description": "Dinner"})
        outbox = await (
            await db._conn.execute(
                "SELECT id FROM sync_outbox WHERE transaction_id=? "
                "AND completed_at IS NULL",
                (tx_id,),
            )
        ).fetchone()
        await db.mark_notion_sync_failure(
            outbox["id"], "offline", "2099-01-01T00:00:00+00:00"
        )
        await db.update_transaction(7, tx_id, {"merchant": "Cafe"})
        fenced = await (
            await db._conn.execute(
                "SELECT completed_at FROM sync_outbox "
                "WHERE id=?",
                (outbox["id"],),
            )
        ).fetchone()
        assert fenced["completed_at"] is not None
        refreshed = await (
            await db._conn.execute(
                "SELECT attempt_count,next_attempt_at,last_error FROM sync_outbox "
                "WHERE transaction_id=? AND completed_at IS NULL",
                (tx_id,),
            )
        ).fetchone()
        assert dict(refreshed) == {
            "attempt_count": 0,
            "next_attempt_at": None,
            "last_error": None,
        }
        resolved = await db.find_transaction_by_notion_page_id(7, "abcd1234")
        assert resolved["id"] == row["id"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_void_resolves_upsert_enqueues_archive_and_is_idempotent(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        row, changed = await db.void_transaction(7, tx_id)
        assert changed is True and row["status"] == "voided"
        row, changed = await db.void_transaction(7, tx_id)
        assert changed is False and row["status"] == "voided"
        events = await (await db._conn.execute(
            "SELECT event_type FROM transaction_events WHERE transaction_id=?", (tx_id,)
        )).fetchall()
        assert [event["event_type"] for event in events].count("voided") == 1
        jobs = await (await db._conn.execute(
            "SELECT operation,completed_at FROM sync_outbox WHERE transaction_id=? ORDER BY id", (tx_id,)
        )).fetchall()
        assert jobs[0]["operation"] == "upsert" and jobs[0]["completed_at"] is not None
        assert jobs[1]["operation"] == "archive" and jobs[1]["completed_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_transaction_validates_and_scopes_user(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        with pytest.raises(ValueError):
            await db.update_transaction(7, tx_id, {"amount_idr": 0})
        with pytest.raises(ValueError):
            await db.update_transaction(7, tx_id, {"status": "voided"})
        row, changed = await db.update_transaction(8, tx_id, {"description": "nope"})
        assert row is None and changed is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_update_transaction_rejects_stale_server_revision(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        current = (await db.list_transactions(7))[0]
        with pytest.raises(TransactionConflictError):
            await db.update_transaction(
                7,
                tx_id,
                {"description": "stale edit"},
                expected_updated_at="2000-01-01T00:00:00+00:00",
            )
        assert (await db.list_transactions(7))[0]["description"] == current["description"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_void_transaction_rejects_stale_server_revision(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        current = (await db.list_transactions(7))[0]
        with pytest.raises(TransactionConflictError):
            await db.void_transaction(
                7,
                tx_id,
                expected_updated_at="2000-01-01T00:00:00+00:00",
            )
        assert (await db.list_transactions(7))[0]["status"] == current["status"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_edit_fences_an_inflight_outbox_snapshot(tmp_path):
    db = await make_db(tmp_path)
    try:
        tx_id = await make_transaction(db)
        old_job = await (
            await db._conn.execute(
                "SELECT id,revision FROM sync_outbox WHERE transaction_id=?", (tx_id,)
            )
        ).fetchone()

        await db.update_transaction(7, tx_id, {"description": "Dinner"})
        jobs = await (
            await db._conn.execute(
                "SELECT id,revision,completed_at FROM sync_outbox "
                "WHERE transaction_id=? ORDER BY id",
                (tx_id,),
            )
        ).fetchall()
        assert len(jobs) == 2
        assert jobs[0]["id"] == old_job["id"] and jobs[0]["completed_at"] is not None
        assert jobs[1]["revision"] == old_job["revision"] + 1
        assert jobs[1]["completed_at"] is None

        # Completion from the old worker may retain the remote page ID, but it
        # must not close the revision created by the edit.
        await db.mark_notion_sync_success(old_job["id"], "remote-page-1")
        await db.mark_notion_sync_failure(old_job["id"], "late failure")
        fresh = await (
            await db._conn.execute(
                "SELECT completed_at,attempt_count FROM sync_outbox WHERE id=?",
                (jobs[1]["id"],),
            )
        ).fetchone()
        assert dict(fresh) == {"completed_at": None, "attempt_count": 0}
        transaction = await (
            await db._conn.execute(
                "SELECT notion_page_id FROM transactions WHERE id=?", (tx_id,)
            )
        ).fetchone()
        assert transaction["notion_page_id"] == "remote-page-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ledger_canonicalizes_dates_and_cannot_confirm_transfer(tmp_path):
    db = await make_db(tmp_path)
    try:
        ingested, _ = await db.create_ingested_transaction(
            7, kind="expense", amount_idr=1, occurred_on="20260729",
            description="x", source_ref="canonical:ingested",
        )
        assert ingested["occurred_on"] == "2026-07-29"
        confirmed, _ = await db.create_confirmed_external_transaction(
            7, kind="income", amount_idr=1, occurred_on="20260730",
            description="x", source="bank_email", source_ref="canonical:external",
        )
        assert confirmed["occurred_on"] == "2026-07-30"
        updated, _ = await db.update_transaction(
            7, confirmed["id"], {"occurred_on": "20260731"}
        )
        assert updated["occurred_on"] == "2026-07-31"

        transfer, _ = await db.create_ingested_transaction(
            7, kind="transfer", amount_idr=1, occurred_on="2026-07-29",
            description="move", source_ref="transfer:pending",
        )
        with pytest.raises(ValueError, match="Transfer transactions"):
            await db.confirm_transaction(7, transfer["id"])
        jobs = await (
            await db._conn.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE transaction_id=?", (transfer["id"],)
            )
        ).fetchone()
        assert jobs[0] == 0
    finally:
        await db.close()
