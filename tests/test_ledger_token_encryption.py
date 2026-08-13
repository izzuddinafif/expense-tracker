from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from db import Database


@pytest.mark.asyncio
async def test_new_notion_token_requires_encryption_key_but_legacy_plaintext_reads(tmp_path, monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    db = await Database.connect(str(tmp_path / "tokens.db"))
    try:
        with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY is required"):
            await db.upsert_user(7, owner_name="Afif", notion_token="new-token")

        now = datetime.now(timezone.utc).isoformat()
        await db._conn.execute(
            "INSERT INTO users (telegram_id,owner_name,notion_token,setup_step,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (8, "Legacy", "legacy-plaintext", "done", now, now),
        )
        await db._conn.commit()
        user = await db.get_user(8)
        assert user is not None and user.notion_token == "legacy-plaintext"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_valid_key_encrypts_tokens_and_invalid_key_fails_closed(tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", key)
    db = await Database.connect(str(tmp_path / "encrypted.db"))
    try:
        await db.upsert_user(7, owner_name="Afif", notion_token="secret-token")
        stored = await (
            await db._conn.execute("SELECT notion_token FROM users WHERE telegram_id=7")
        ).fetchone()
        assert stored["notion_token"].startswith("enc:")
        assert (await db.get_user(7)).notion_token == "secret-token"
    finally:
        await db.close()

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(ValueError, match="Invalid TOKEN_ENCRYPTION_KEY"):
        await Database.connect(str(tmp_path / "bad-key.db"))


@pytest.mark.asyncio
async def test_startup_encrypts_legacy_plaintext_tokens_once(tmp_path, monkeypatch):
    path = tmp_path / "legacy-token.db"
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    initial = await Database.connect(str(path))
    try:
        now = datetime.now(timezone.utc).isoformat()
        await initial._conn.execute(
            "INSERT INTO users (telegram_id,owner_name,notion_token,setup_step,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (7, "Afif", "legacy-token", "done", now, now),
        )
        await initial._conn.commit()
    finally:
        await initial.close()

    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    migrated = await Database.connect(str(path))
    try:
        first = await (
            await migrated._conn.execute(
                "SELECT notion_token FROM users WHERE telegram_id=7"
            )
        ).fetchone()
        assert first["notion_token"].startswith("enc:")
        assert (await migrated.get_user(7)).notion_token == "legacy-token"
    finally:
        await migrated.close()

    reopened = await Database.connect(str(path))
    try:
        second = await (
            await reopened._conn.execute(
                "SELECT notion_token FROM users WHERE telegram_id=7"
            )
        ).fetchone()
        assert second["notion_token"] == first["notion_token"]
    finally:
        await reopened.close()
