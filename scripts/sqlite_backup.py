"""Safe online SQLite backup and restore utility.

The utility deliberately requires all paths from the caller.  Backups use
SQLite's online ``Connection.backup`` API, so the source database may remain
in use while it is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
import re
from pathlib import Path


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be safely completed."""


_BACKUP_TIMESTAMP = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{8}T\d{12}Z)(?P<suffix>\.[^.]+)$")


def _backup_candidates(source: Path, destination_dir: Path) -> list[tuple[datetime, Path]]:
    """Return only regular files matching this utility's exact backup format."""
    prefix = f"{source.stem}-"
    suffix = source.suffix or ".sqlite3"
    candidates: list[tuple[datetime, Path]] = []
    if not destination_dir.is_dir():
        return candidates
    for path in destination_dir.iterdir():
        if path.is_symlink() or not path.is_file() or not path.name.startswith(prefix):
            continue
        match = _BACKUP_TIMESTAMP.match(path.name)
        if not match or match.group("stem") != source.stem or match.group("suffix") != suffix:
            continue
        try:
            stamp = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        candidates.append((stamp, path))
    return sorted(candidates, key=lambda item: (item[0], item[1].name), reverse=True)


def select_retained_backups(
    source: Path,
    destination_dir: Path,
    *,
    daily_generations: int = 7,
    weekly_generations: int = 4,
) -> tuple[list[Path], list[Path]]:
    """Return ``(retained, removable)`` using deterministic UTC day/week buckets."""
    if daily_generations < 0 or weekly_generations < 0:
        raise ValueError("retention generations cannot be negative")
    candidates = _backup_candidates(Path(source), Path(destination_dir))
    keep: set[Path] = set()
    daily_seen: set[object] = set()
    weekly_seen: set[object] = set()
    for stamp, path in candidates:
        day = stamp.date()
        if len(daily_seen) < daily_generations and day not in daily_seen:
            daily_seen.add(day)
            keep.add(path)
        week = stamp.isocalendar()[:2]
        if len(weekly_seen) < weekly_generations and week not in weekly_seen:
            weekly_seen.add(week)
            keep.add(path)
    retained = [path for _, path in candidates if path in keep]
    removable = [path for _, path in candidates if path not in keep]
    return retained, removable


def maintain_backups(
    source: Path,
    destination_dir: Path,
    *,
    daily_generations: int = 7,
    weekly_generations: int = 4,
    dry_run: bool = False,
) -> dict[str, object]:
    """Create a verified backup and prune only matching old generations."""
    source = Path(source)
    destination_dir = Path(destination_dir)
    if daily_generations < 0 or weekly_generations < 0:
        raise BackupError("retention generations cannot be negative")
    created: Path | None = None
    if not dry_run:
        created = backup_database(source, destination_dir)
    retained, removable = select_retained_backups(
        source,
        destination_dir,
        daily_generations=daily_generations,
        weekly_generations=weekly_generations,
    )
    deleted: list[str] = []
    if not dry_run:
        for path in removable:
            # Re-check the exact candidate before deletion to avoid deleting a
            # file replaced by a concurrent process or symlink.
            if path.is_symlink() or not path.is_file() or path not in {
                p for _, p in _backup_candidates(source, destination_dir)
            }:
                continue
            path.unlink()
            sidecar = _metadata_path(path)
            if sidecar.is_file() and not sidecar.is_symlink():
                sidecar.unlink()
            deleted.append(str(path))
    return {
        "source": str(source),
        "destination": str(destination_dir),
        "created": str(created) if created else None,
        "dry_run": dry_run,
        "retained": [str(path) for path in retained],
        "deleted": deleted if not dry_run else [str(path) for path in removable],
        "candidates": len(retained) + len(removable),
    }


def integrity_check(path: Path) -> None:
    """Raise :class:`BackupError` unless SQLite reports a healthy database."""

    try:
        with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"integrity check failed for {path}: {exc}") from exc
    if not result or result[0] != "ok":
        detail = result[0] if result else "no result"
        raise BackupError(f"integrity check failed for {path}: {detail}")


def _timestamped_name(source: Path) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{source.stem}-{stamp}{source.suffix or '.sqlite3'}"


def _metadata_path(backup: Path) -> Path:
    return backup.with_suffix(backup.suffix + ".json")


def _record_backup_heartbeat(
    source: Path,
    destination: Path,
    *,
    success: bool,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Best-effort health update when the source uses the app schema."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(source, timeout=5) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='operational_state'"
            ).fetchone()
            if table is None:
                return
            payload = {"path": str(destination), **(metadata or {})}
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(operational_state)")
            }
            if "consecutive_failures" in columns and success:
                conn.execute(
                    "INSERT INTO operational_state "
                    "(name,started_at,last_heartbeat_at,last_attempt_at,last_success_at,"
                    "last_error,consecutive_failures,metadata_json,updated_at) "
                    "VALUES ('backup',?,?,?,?,NULL,0,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "last_heartbeat_at=excluded.last_heartbeat_at,"
                    "last_attempt_at=excluded.last_attempt_at,"
                    "last_success_at=excluded.last_success_at,last_error=NULL,"
                    "consecutive_failures=0,metadata_json=excluded.metadata_json,"
                    "updated_at=excluded.updated_at",
                    (now, now, now, now, json.dumps(payload), now),
                )
            elif "consecutive_failures" in columns:
                conn.execute(
                    "INSERT INTO operational_state "
                    "(name,started_at,last_heartbeat_at,last_attempt_at,last_success_at,"
                    "last_error,consecutive_failures,metadata_json,updated_at) "
                    "VALUES ('backup',?,?,?,?,?,1,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "last_heartbeat_at=excluded.last_heartbeat_at,"
                    "last_attempt_at=excluded.last_attempt_at,"
                    "last_error=excluded.last_error,"
                    "consecutive_failures=operational_state.consecutive_failures+1,"
                    "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (now, now, now, None, error or "backup failed", json.dumps(payload), now),
                )
            elif success:
                conn.execute(
                    "INSERT INTO operational_state "
                    "(name,last_attempt_at,last_success_at,last_error,metadata_json,updated_at) "
                    "VALUES ('backup',?,?,NULL,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "last_attempt_at=excluded.last_attempt_at,"
                    "last_success_at=excluded.last_success_at,last_error=NULL,"
                    "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (now, now, json.dumps(payload), now),
                )
            else:
                conn.execute(
                    "INSERT INTO operational_state "
                    "(name,last_attempt_at,last_success_at,last_error,metadata_json,updated_at) "
                    "VALUES ('backup',?,NULL,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET "
                    "last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error,"
                    "metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (now, error or "backup failed", json.dumps(payload), now),
                )
    except sqlite3.Error:
        # Backup validity is authoritative; missing health metadata must not
        # turn a valid recovery artifact into a failed backup command.
        return


def backup_database(source: Path, destination_dir: Path, *, overwrite: bool = False) -> Path:
    """Create a timestamped online backup under ``destination_dir``."""

    source = Path(source)
    destination_dir = Path(destination_dir)
    if not source.is_file():
        raise BackupError(f"source database does not exist: {source}")
    destination = destination_dir / _timestamped_name(source)
    temp_path: Path | None = None
    temp_metadata_path: Path | None = None
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            raise BackupError(f"refusing to overwrite existing backup: {destination}")
        fd, temp_name = tempfile.mkstemp(
            dir=destination_dir, prefix=f".{destination.name}.", suffix=".tmp"
        )
        os.close(fd)
        temp_path = Path(temp_name)
        with sqlite3.connect(source) as src, sqlite3.connect(temp_path) as dst:
            src.backup(dst)
        integrity_check(temp_path)
        digest = hashlib.sha256()
        with temp_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        metadata = {
            "backup": destination.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": temp_path.stat().st_size,
            "sha256": digest.hexdigest(),
            "source": str(source),
        }
        metadata_path = _metadata_path(destination)
        metadata_fd, metadata_name = tempfile.mkstemp(
            dir=destination_dir, prefix=f".{metadata_path.name}.", suffix=".tmp"
        )
        temp_metadata_path = Path(metadata_name)
        with os.fdopen(metadata_fd, "w", encoding="utf-8") as metadata_stream:
            json.dump(metadata, metadata_stream, sort_keys=True)
            metadata_stream.write("\n")
            metadata_stream.flush()
            os.fsync(metadata_stream.fileno())
        if destination.exists() and not overwrite:
            raise BackupError(f"refusing to overwrite existing backup: {destination}")
        os.replace(temp_path, destination)
        temp_path = None
        os.replace(temp_metadata_path, metadata_path)
        temp_metadata_path = None
        try:
            dir_fd = os.open(destination_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        _record_backup_heartbeat(
            source,
            destination,
            success=True,
            metadata={"size_bytes": destination.stat().st_size, "sha256": digest.hexdigest()},
        )
        return destination
    except (sqlite3.Error, OSError, BackupError) as exc:
        _record_backup_heartbeat(source, destination, success=False, error=str(exc))
        raise BackupError(f"backup failed: {exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if temp_metadata_path is not None:
            temp_metadata_path.unlink(missing_ok=True)


def restore_database(backup: Path, target: Path, *, allow_existing: bool = False) -> Path:
    """Restore ``backup`` into ``target`` after validating the backup.

    Existing targets (including databases with SQLite WAL/journal sidecars)
    are refused unless ``allow_existing`` is explicitly set by the operator.
    """

    backup = Path(backup)
    target = Path(target)
    if not backup.is_file():
        raise BackupError(f"backup does not exist: {backup}")
    sidecars = (Path(f"{target}-wal"), Path(f"{target}-journal"), Path(f"{target}-shm"))
    if (target.exists() or any(path.exists() for path in sidecars)) and not allow_existing:
        raise BackupError(f"refusing to restore into existing target: {target}")
    if not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    integrity_check(backup)

    try:
        with sqlite3.connect(backup) as src, sqlite3.connect(target) as dst:
            src.backup(dst)
    except sqlite3.Error as exc:
        raise BackupError(f"restore failed: {exc}") from exc
    integrity_check(target)
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely back up and restore SQLite databases")
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser("backup", help="create a timestamped online backup")
    backup.add_argument("--source", type=Path, required=True, help="source SQLite database")
    backup.add_argument("--destination", type=Path, required=True, help="directory for the backup")
    backup.add_argument("--overwrite", action="store_true", help="allow replacing a colliding backup name")

    restore = commands.add_parser("restore", help="restore a backup into a database")
    restore.add_argument("--source", type=Path, required=True, help="backup SQLite database")
    restore.add_argument("--destination", type=Path, required=True, help="target SQLite database")
    restore.add_argument("--allow-existing", action="store_true", help="allow replacing an existing/running target")

    maintain = commands.add_parser(
        "maintain", help="create a verified backup and prune old generations"
    )
    maintain.add_argument("--source", type=Path, required=True, help="source SQLite database")
    maintain.add_argument("--destination", type=Path, required=True, help="backup directory")
    maintain.add_argument("--daily", type=int, default=7, help="daily generations to retain")
    maintain.add_argument("--weekly", type=int, default=4, help="weekly generations to retain")
    maintain.add_argument("--dry-run", action="store_true", help="report changes without writing or deleting")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        # Keep the callable entry point testable while preserving argparse's
        # normal exit status when invoked as a command.
        return int(exc.code)
    try:
        if args.command == "backup":
            result = backup_database(args.source, args.destination, overwrite=args.overwrite)
        elif args.command == "restore":
            result = restore_database(args.source, args.destination, allow_existing=args.allow_existing)
        else:
            result = maintain_backups(
                args.source,
                args.destination,
                daily_generations=args.daily,
                weekly_generations=args.weekly,
                dry_run=args.dry_run,
            )
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if isinstance(result, dict):
        print(json.dumps(result, sort_keys=True))
    else:
        print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
