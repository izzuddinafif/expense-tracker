import json
import sqlite3

import pytest

from scripts.sqlite_backup import (
    BackupError,
    main,
    maintain_backups,
    select_retained_backups,
)


def make_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE ledger (amount INTEGER NOT NULL)")
        conn.execute("INSERT INTO ledger VALUES (1)")


def backup_name(day: str, time: str = "120000000000") -> str:
    return f"source-{day}T{time}Z.sqlite3"


def test_selection_keeps_daily_and_weekly_union_and_ignores_unrelated(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backups"
    destination.mkdir()
    names = [
        backup_name("20260729"),
        backup_name("20260728"),
        backup_name("20260727"),
        backup_name("20260720"),
        backup_name("20260713"),
        "source-not-a-backup.sqlite3",
        "other-20260701T120000000000Z.sqlite3",
    ]
    for name in names:
        (destination / name).write_bytes(b"placeholder")

    retained, removable = select_retained_backups(
        source, destination, daily_generations=2, weekly_generations=2
    )
    assert [path.name for path in retained] == [backup_name("20260729"), backup_name("20260728"), backup_name("20260720")]
    assert [path.name for path in removable] == [backup_name("20260727"), backup_name("20260713")]
    assert (destination / "source-not-a-backup.sqlite3").exists()


def test_dry_run_does_not_create_or_delete(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    destination = tmp_path / "backups"
    result = maintain_backups(source, destination, daily_generations=1, weekly_generations=1, dry_run=True)
    assert result["dry_run"] is True
    assert result["created"] is None
    assert not destination.exists()


def test_maintain_creates_verified_backup_and_prunes_exact_candidates(tmp_path):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    destination = tmp_path / "backups"
    old = destination / backup_name("20200101")
    destination.mkdir()
    old.write_bytes(b"old")
    unrelated = destination / "notes.txt"
    unrelated.write_text("keep")

    result = maintain_backups(source, destination, daily_generations=1, weekly_generations=0)
    assert result["created"]
    assert not old.exists()
    assert unrelated.exists()
    assert len(list(destination.glob("source-*.sqlite3"))) == 1


def test_maintain_rejects_negative_retention(tmp_path):
    source = tmp_path / "source.sqlite3"
    with pytest.raises(BackupError, match="cannot be negative"):
        maintain_backups(source, tmp_path / "backups", daily_generations=-1)


def test_maintain_cli_emits_json(tmp_path, capsys):
    source = tmp_path / "source.sqlite3"
    make_db(source)
    assert main(
        ["maintain", "--source", str(source), "--destination", str(tmp_path / "backups"), "--dry-run"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["source"] == str(source)
