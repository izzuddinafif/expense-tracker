from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import cast

import pytest

_previous_log_path = os.environ.get("BOT_LOG_PATH")
os.environ["BOT_LOG_PATH"] = str(
    Path(tempfile.gettempdir()) / f"expense-tracker-setup-test-{os.getpid()}.log"
)

import main
from db import Database

if _previous_log_path is None:
    os.environ.pop("BOT_LOG_PATH", None)
else:
    os.environ["BOT_LOG_PATH"] = _previous_log_path


TOKEN = "ntn_setup_secret_value"


@dataclass
class FakeUser:
    notion_token: str = ""
    setup_step: str = "await_token"


class FakeDb:
    def __init__(self):
        self.user = FakeUser()
        self.events = []

    async def get_user(self, user_id):
        self.events.append(("get_user", user_id))
        return FakeUser(self.user.notion_token, self.user.setup_step)

    async def upsert_user(self, user_id, **fields):
        self.events.append(("upsert_user", user_id, fields.copy()))
        for name, value in fields.items():
            if hasattr(self.user, name):
                setattr(self.user, name, value)

    async def set_user_setup_step(self, user_id, step):
        self.events.append(("set_step", user_id, step))
        self.user.setup_step = step


class FakeMessage:
    def __init__(self, events, *, delete_error=None):
        self.events = events
        self.delete_error = delete_error
        self.answers = []

    async def delete(self):
        self.events.append(("delete",))
        if self.delete_error is not None:
            raise self.delete_error

    async def answer(self, text, **kwargs):
        self.events.append(("answer", text))
        self.answers.append((text, kwargs))


@pytest.mark.asyncio
async def test_setup_token_is_stored_then_deleted_before_successful_discovery(monkeypatch):
    db = FakeDb()
    msg = FakeMessage(db.events)
    initialized = []

    class SuccessfulNotionClient:
        def __init__(self, notion_token, db_ids):
            assert notion_token == TOKEN
            assert db_ids == {}
            db.events.append(("client",))

        async def discover_databases(self):
            db.events.append(("discover",))
            return {"expenses_ds": "expenses-id", "income_ds": "income-id"}

        async def aclose(self):
            db.events.append(("close",))

    async def initialize_user(user_id):
        initialized.append(user_id)

    monkeypatch.setattr(main, "NotionClient", SuccessfulNotionClient)

    await main._accept_setup_token(
        cast(object, msg),
        7,
        TOKEN,
        cast(Database, db),
        initialize_user,
    )

    assert db.events[0][0] == "upsert_user"
    assert db.events[1] == ("delete",)
    assert next(i for i, event in enumerate(db.events) if event[0] == "discover") > 1
    assert db.user.setup_step == "done"
    assert initialized == [7]


@pytest.mark.asyncio
async def test_failed_discovery_keeps_retry_state_without_exposing_token(monkeypatch, caplog):
    db = FakeDb()
    msg = FakeMessage(db.events)
    initialized = []

    class FailingNotionClient:
        def __init__(self, notion_token, db_ids):
            assert notion_token == TOKEN

        async def discover_databases(self):
            raise RuntimeError(f"authorization failed for {TOKEN}")

        async def aclose(self):
            raise AssertionError("not reached")

    async def initialize_user(user_id):
        initialized.append(user_id)

    monkeypatch.setattr(main, "NotionClient", FailingNotionClient)

    await main._accept_setup_token(
        cast(object, msg),
        7,
        TOKEN,
        cast(Database, db),
        initialize_user,
    )

    assert db.events[1] == ("delete",)
    assert db.user.setup_step == "await_token"
    assert initialized == []
    assert TOKEN not in caplog.text
    assert all(TOKEN not in text for text, _kwargs in msg.answers)
    assert "RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_token_message_delete_failure_is_audited_without_blocking_discovery(
    monkeypatch, caplog
):
    db = FakeDb()
    msg = FakeMessage(db.events, delete_error=RuntimeError(TOKEN))

    class SuccessfulNotionClient:
        def __init__(self, notion_token, db_ids):
            assert notion_token == TOKEN

        async def discover_databases(self):
            return {"expenses_ds": "expenses-id"}

        async def aclose(self):
            return None

    async def initialize_user(_user_id):
        return None

    monkeypatch.setattr(main, "NotionClient", SuccessfulNotionClient)

    await main._accept_setup_token(
        cast(object, msg),
        7,
        TOKEN,
        cast(Database, db),
        initialize_user,
    )

    assert db.user.setup_step == "done"
    assert "Could not delete setup token message for user 7 (RuntimeError)" in caplog.text
    assert TOKEN not in caplog.text
