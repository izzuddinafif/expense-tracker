from types import SimpleNamespace
from typing import cast

import pytest

from db import Database
from email_watcher import EmailWatcher
from models import EmailTransaction, NotionCache


@pytest.mark.asyncio
async def test_message_failure_is_persisted_without_blocking_next_email():
    failures = []
    processed = []

    class FakeDb:
        async def is_processed(self, _uid):
            return False

        async def record_email_processing_failure(self, uid, sender, error):
            failures.append((uid, sender, error))
            return {"attempt_count": 1, "status": "retrying"}

        async def mark_processed(self, uid, sender):
            processed.append((uid, sender))

    class FakeAgent:
        async def parse_bank_email(self, *, subject, **_kwargs):
            if subject == "bad":
                raise RuntimeError("model unavailable")
            return EmailTransaction(
                type="skip",
                description="",
                amount=0,
                date="2026-07-29",
                subcategory="",
                account="",
                skip_reason="not a transaction",
            )

    watcher = EmailWatcher(
        config=SimpleNamespace(email_poll_interval=300),
        db=cast(Database, FakeDb()),
        notion=object(),
        agent=FakeAgent(),
        cache_getter=NotionCache,
    )

    await watcher._process_one("uid-bad", "bank@example.test", "bad", "body")
    await watcher._process_one("uid-good", "bank@example.test", "good", "body")

    assert failures[0][0:2] == ("uid-bad", "bank@example.test")
    assert "parse" in failures[0][2]
    assert processed == [("uid-good", "bank@example.test")]
