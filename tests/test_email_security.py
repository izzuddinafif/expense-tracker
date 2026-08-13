from types import SimpleNamespace
from typing import cast

import pytest

from email_watcher import BANK_SENDERS, EmailWatcher
from models import EmailTransaction, NotionCache


class FakeDb:
    def __init__(self):
        self.failures = []
        self.processed = []

    async def record_email_processing_failure(self, uid, sender, reason):
        self.failures.append((uid, sender, reason))
        return {"attempt_count": 1, "status": "retrying"}

    async def mark_processed(self, uid, sender):
        self.processed.append((uid, sender))

    async def get_email_owner_for_account(self, account):
        return None


class SkipAgent:
    def __init__(self, tx=None):
        self.calls = 0
        self.tx = tx or EmailTransaction(
            type="skip", description="", amount=0, date="2026-07-30",
            subcategory="", account="", skip_reason="test",
        )

    async def parse_bank_email(self, **kwargs):
        self.calls += 1
        return self.tx


def watcher(db, agent, cache):
    return EmailWatcher(
        config=SimpleNamespace(), db=cast(object, db), notion=object(),
        agent=agent, cache_getter=lambda: cache,
    )


@pytest.mark.asyncio
async def test_authentication_failure_and_from_mismatch_are_rejected_without_model():
    for sender, auth in [
        (BANK_SENDERS[0], "dmarc=fail; dkim=pass header.d=bankmandiri.co.id"),
        ("spoof@example.test", "dmarc=pass; dkim=pass header.d=example.test"),
    ]:
        db = FakeDb()
        agent = SkipAgent()
        await watcher(db, agent, NotionCache())._process("u1", sender, "ok", "body", auth)
        assert agent.calls == 0
        assert db.processed == [("u1", sender)]
        assert db.failures


@pytest.mark.asyncio
async def test_aligned_authentication_allows_model_call():
    db = FakeDb()
    agent = SkipAgent()
    auth = "dmarc=pass; dkim=pass header.d=bankmandiri.co.id"
    await watcher(db, agent, NotionCache())._process("u2", BANK_SENDERS[0], "ok", "body", auth)
    assert agent.calls == 1
    assert db.processed == [("u2", BANK_SENDERS[0])]


@pytest.mark.asyncio
async def test_missing_or_ambiguous_account_is_rejected():
    tx = SimpleNamespace(
        type="expense", description="Shop", amount=1000, date="2026-07-30",
        subcategory="Food", account="", merchant="Shop",
    )
    db = FakeDb()
    agent = SkipAgent(tx)
    await watcher(db, agent, NotionCache(accounts={"Jago": "1"}))._process("u3", BANK_SENDERS[1], "ok", "body")
    assert db.processed == [("u3", BANK_SENDERS[1])]
    assert "account" in db.failures[-1][2]

    tx.account = "jago"
    db = FakeDb()
    await watcher(db, SkipAgent(tx), NotionCache(accounts={"Jago": "1", "jago": "2"}))._process("u4", BANK_SENDERS[1], "ok", "body")
    assert db.processed == [("u4", BANK_SENDERS[1])]
    assert "account" in db.failures[-1][2]


@pytest.mark.parametrize("payload", [
    {"type": "bogus", "amount": 1},
    {"type": "expense", "amount": 1.5},
    {"type": "expense", "amount": -1},
    {"type": "expense", "amount": 1, "date": "30-07-2026"},
    {"type": "expense", "amount": "1"},
    {"type": "expense", "amount": True},
    {"type": "expense", "amount": 1, "admin_fee": "2"},
])
def test_email_transaction_rejects_invalid_fields(payload):
    base = dict(description="x", admin_fee=0, date="2026-07-30", subcategory="Food", account="Jago")
    with pytest.raises(ValueError):
        EmailTransaction(**(base | payload))


def test_email_body_redaction_removes_sensitive_identifiers():
    from agent import Agent

    redacted = Agent._redact_email_content(
        "Contact user@example.com, account 1234 5678 9012, https://evil.test/path"
    )
    assert "user@example.com" not in redacted
    assert "1234 5678 9012" not in redacted
    assert "https://evil.test" not in redacted
