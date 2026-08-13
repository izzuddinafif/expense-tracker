import asyncio

import pytest

from db import Database


@pytest.mark.asyncio
async def test_shared_connection_serializes_email_writes(tmp_path):
    db = await Database.connect(str(tmp_path / "concurrency.db"))
    try:
        await asyncio.gather(
            db.mark_processed("uid-processed", "bank@example.test"),
            db.mark_rejected("uid-rejected", "spoof@example.test", "rejected"),
            *(
                db.record_email_processing_failure(
                    "uid-retry", "bank@example.test", "temporary"
                )
                for _ in range(8)
            ),
        )

        assert await db.is_processed("uid-processed")
        assert await db.is_processed("uid-rejected")
        failure = (await db.list_email_processing_failures())[0]
        assert failure["uid"] == "uid-retry"
        assert failure["attempt_count"] == 8
        assert failure["status"] == "terminal"
    finally:
        await db.close()
