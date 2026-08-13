import sqlite3
import json

import pytest

from scripts.sqlite_backup import (
    BackupError,
    backup_database,
    integrity_check,
    main,
    record_backup_status,
    restore_database,
)


def make_db(path, value="before"):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        conn.execute("INSERT INTO values_table VALUES (?)", (value,))


def test_online_backup_is_timestamped_and_valid(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    destination = backup_database(source, tmp_path / "backups")
    assert destination.parent.name == "backups"
    assert destination.name.startswith("source-")
    integrity_check(destination)
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT value FROM values_table").fetchone()[0] == "before"
    metadata = json.loads(destination.with_suffix(destination.suffix + ".json").read_text())
    assert metadata["backup"] == destination.name
    assert metadata["size_bytes"] == destination.stat().st_size
    assert len(metadata["sha256"]) == 64
    assert not list(destination.parent.glob(f".{destination.name}.*.tmp-*"))


def test_restore_refuses_existing_target_without_explicit_flag(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source, "restored")
    backup = backup_database(source, tmp_path / "backups")
    target = tmp_path / "target.sqlite3"
    make_db(target, "keep")
    with pytest.raises(BackupError, match="existing target"):
        restore_database(backup, target)
    restore_database(backup, target, allow_existing=True)
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT value FROM values_table").fetchone()[0] == "restored"


def test_cli_help_and_missing_source(capsys, tmp_path):
    assert main(["backup", "--help"]) == 0
    assert "--source" in capsys.readouterr().out
    assert main(["backup", "--source", str(tmp_path / "missing"), "--destination", str(tmp_path)]) == 2


def test_backup_records_heartbeat_when_app_schema_is_present(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "CREATE TABLE operational_state ("
            "name TEXT PRIMARY KEY,last_attempt_at TEXT,last_success_at TEXT,"
            "last_error TEXT,metadata_json TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
    backup = backup_database(source, tmp_path / "backups")
    with sqlite3.connect(source) as conn:
        row = conn.execute(
            "SELECT last_success_at,last_error,metadata_json FROM operational_state "
            "WHERE name='backup'"
        ).fetchone()
    assert row[0]
    assert row[1] is None
    metadata = json.loads(row[2])
    assert metadata["size_bytes"] == backup.stat().st_size
    assert len(metadata["sha256"]) == 64


def test_failed_backup_records_attempt_and_cleans_temp_file(tmp_path, monkeypatch):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "CREATE TABLE operational_state ("
            "name TEXT PRIMARY KEY,last_attempt_at TEXT,last_success_at TEXT,"
            "last_error TEXT,metadata_json TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
    import scripts.sqlite_backup as backup_module

    def fail_check(_path):
        raise BackupError("simulated integrity failure")

    monkeypatch.setattr(backup_module, "integrity_check", fail_check)
    destination_dir = tmp_path / "backups"
    with pytest.raises(BackupError, match="simulated integrity failure"):
        backup_database(source, destination_dir)
    assert list(destination_dir.glob("*.tmp")) == []
    with sqlite3.connect(source) as conn:
        row = conn.execute(
            "SELECT last_attempt_at,last_success_at,last_error FROM operational_state "
            "WHERE name='backup'"
        ).fetchone()
    assert row[0]
    assert row[1] is None
    assert "simulated integrity failure" in row[2]


def test_offsite_status_overrides_local_success_health(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute(
            "CREATE TABLE operational_state ("
            "name TEXT PRIMARY KEY,last_attempt_at TEXT,last_success_at TEXT,"
            "last_error TEXT,metadata_json TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )

    backup = tmp_path / "backups" / "source-20260814T000000000000Z.sqlite3"
    record_backup_status(
        source,
        backup,
        success=True,
        metadata={"offsite_host": "backup.example"},
    )
    record_backup_status(
        source,
        backup,
        success=False,
        error="remote checksum mismatch",
        metadata={"offsite_host": "backup.example"},
    )

    with sqlite3.connect(source) as conn:
        row = conn.execute(
            "SELECT last_success_at,last_error,metadata_json FROM operational_state "
            "WHERE name='backup'"
        ).fetchone()
    assert row[0]
    assert row[1] == "remote checksum mismatch"
    assert json.loads(row[2])["offsite_host"] == "backup.example"
