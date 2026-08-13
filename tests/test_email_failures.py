from datetime import datetime, timedelta, timezone

import pytest

from db import Database


@pytest.mark.asyncio
async def test_email_failure_transitions_and_terminal_exclusion(tmp_path):
    db = await Database.connect(str(tmp_path / "email-failures.db"))
    try:
        first = await db.record_email_processing_failure(
            "uid-1", "bank@example.test", "parse failed"
        )
        assert first["attempt_count"] == 1
        assert first["status"] == "retrying"
        for _ in range(2):
            third = await db.record_email_processing_failure(
                "uid-1", "bank@example.test", "parse failed"
            )
        assert third["attempt_count"] == 3
        assert third["status"] == "degraded"
        for _ in range(5):
            eighth = await db.record_email_processing_failure(
                "uid-1", "bank@example.test", "parse failed"
            )
        assert eighth["attempt_count"] == 8
        assert eighth["status"] == "terminal"
        assert "uid-1" in await db.get_email_excluded_uids()
        assert await db.get_email_failure_summary() == {
            "retrying": 0,
            "degraded": 0,
            "terminal": 1,
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_email_failure_terminalizes_after_24_hours_and_survives_reopen(tmp_path):
    path = tmp_path / "email-failures.db"
    start = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db = await Database.connect(str(path))
    await db.record_email_processing_failure(
        "uid-aged", "bank@example.test", "routing failed", now=start
    )
    await db.close()

    reopened = await Database.connect(str(path))
    try:
        excluded = await reopened.get_email_excluded_uids(
            now=start + timedelta(hours=24, seconds=1)
        )
        assert "uid-aged" in excluded
        row = await (
            await reopened._conn.execute(
                "SELECT status,terminal_at FROM email_processing_failures "
                "WHERE uid='uid-aged'"
            )
        ).fetchone()
        assert row["status"] == "terminal"
        assert row["terminal_at"]
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_success_clears_failure_and_manual_clear_reenables_uid(tmp_path):
    db = await Database.connect(str(tmp_path / "email-failures.db"))
    try:
        for _ in range(8):
            await db.record_email_processing_failure(
                "uid-recovered", "bank@example.test", "temporary failure"
            )
        assert "uid-recovered" in await db.get_email_excluded_uids()
        assert await db.clear_email_processing_failure("uid-recovered")
        assert "uid-recovered" not in await db.get_email_excluded_uids()

        await db.record_email_processing_failure(
            "uid-recovered", "bank@example.test", "one more failure"
        )
        await db.mark_processed("uid-recovered", "bank@example.test")
        assert await db.is_processed("uid-recovered")
        row = await (
            await db._conn.execute(
                "SELECT 1 FROM email_processing_failures WHERE uid='uid-recovered'"
            )
        ).fetchone()
        assert row is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_security_rejection_is_processed_but_remains_auditable(tmp_path):
    db = await Database.connect(str(tmp_path / "email-rejected.db"))
    try:
        await db.mark_rejected("uid-spoof", "spoof@example.test", "authentication rejected")
        assert await db.is_processed("uid-spoof")
        assert "uid-spoof" in await db.get_email_excluded_uids()
        failure = (await db.list_email_processing_failures())[0]
        assert failure["uid"] == "uid-spoof"
        assert failure["status"] == "terminal"
        assert failure["last_error"] == "authentication rejected"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_email_account_owner_cannot_be_silently_replaced(tmp_path):
    db = await Database.connect(str(tmp_path / "email-owners.db"))
    try:
        await db.set_email_account_owner("Mandiri", 7)
        with pytest.raises(ValueError, match="already linked"):
            await db.set_email_account_owner("Mandiri", 8)
        assert await db.get_email_owner_for_account("Mandiri") == 7
    finally:
        await db.close()
