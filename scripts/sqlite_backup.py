"""Safe online SQLite backup and restore utility.

The utility deliberately requires all paths from the caller.  Backups use
SQLite's online ``Connection.backup`` API, so the source database may remain
in use while it is copied.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path


class BackupError(RuntimeError):
    """Raised when a backup or restore cannot be safely completed."""


_BACKUP_TIMESTAMP = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{8}T\d{12}Z)(?P<suffix>\.[^.]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_backup_metadata(
    backup: Path,
    *,
    allow_legacy_without_metadata: bool = False,
) -> dict[str, object] | None:
    """Validate a backup's identity, byte size, and SHA-256 sidecar fields.

    The legacy override applies only when the sidecar is absent. A present but
    malformed or mismatched sidecar always fails closed.
    """

    backup = Path(backup)
    metadata_path = _metadata_path(backup)
    if metadata_path.is_symlink():
        raise BackupError(f"refusing symlinked backup metadata: {metadata_path}")
    if not metadata_path.is_file():
        if allow_legacy_without_metadata:
            warnings.warn(
                f"restoring legacy backup without metadata validation: {backup}",
                RuntimeWarning,
                stacklevel=2,
            )
            return None
        raise BackupError(
            f"backup metadata does not exist: {metadata_path}; "
            "use the explicit legacy override only for a trusted pre-metadata backup"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid backup metadata {metadata_path}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise BackupError(f"invalid backup metadata {metadata_path}: expected an object")
    if metadata.get("backup") != backup.name:
        raise BackupError(f"backup metadata filename mismatch for {backup}")

    expected_size = metadata.get("size_bytes")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
        raise BackupError(f"invalid backup metadata size_bytes for {backup}")
    try:
        actual_size = backup.stat().st_size
    except OSError as exc:
        raise BackupError(f"cannot stat backup {backup}: {exc}") from exc
    if actual_size != expected_size:
        raise BackupError(
            f"backup size mismatch for {backup}: expected {expected_size}, got {actual_size}"
        )

    expected_digest = metadata.get("sha256")
    if not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
        raise BackupError(f"invalid backup metadata sha256 for {backup}")
    try:
        actual_digest = _sha256(backup)
    except OSError as exc:
        raise BackupError(f"cannot hash backup {backup}: {exc}") from exc
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise BackupError(f"backup SHA-256 mismatch for {backup}")
    return metadata


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _record_backup_heartbeat(
    source: Path,
    destination: Path,
    *,
    success: bool,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> bool:
    """Best-effort health update when the source uses the app schema."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(source, timeout=5) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='operational_state'"
            ).fetchone()
            if table is None:
                return False
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
        return True
    except sqlite3.Error:
        # Backup validity is authoritative; missing health metadata must not
        # turn a valid recovery artifact into a failed backup command.
        return False


def record_backup_status(
    source: Path,
    destination: Path,
    *,
    success: bool,
    error: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    """Record the result of a post-backup verification step.

    The host-level off-site uploader calls this after its remote checksums
    pass. A locally valid copy must not make operational health green when the
    required off-site copy is missing or corrupt.
    """

    recorded = _record_backup_heartbeat(
        Path(source),
        Path(destination),
        success=success,
        error=error,
        metadata=metadata,
    )
    if not recorded:
        raise BackupError("could not persist authoritative off-site backup status")


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
        with temp_path.open("rb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        digest = _sha256(temp_path)
        _remove_sqlite_sidecars(temp_path)
        metadata = {
            "backup": destination.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "size_bytes": temp_path.stat().st_size,
            "sha256": digest,
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
            metadata={"size_bytes": destination.stat().st_size, "sha256": digest},
        )
        return destination
    except (sqlite3.Error, OSError, BackupError) as exc:
        _record_backup_heartbeat(source, destination, success=False, error=str(exc))
        raise BackupError(f"backup failed: {exc}") from exc
    finally:
        if temp_path is not None:
            _remove_sqlite_sidecars(temp_path)
            temp_path.unlink(missing_ok=True)
        if temp_metadata_path is not None:
            temp_metadata_path.unlink(missing_ok=True)


def restore_database(
    backup: Path,
    target: Path,
    *,
    allow_existing: bool = False,
    allow_legacy_without_metadata: bool = False,
) -> Path:
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
    validate_backup_metadata(
        backup,
        allow_legacy_without_metadata=allow_legacy_without_metadata,
    )
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
    restore.add_argument(
        "--allow-legacy-without-metadata",
        action="store_true",
        help="restore a trusted legacy backup only when its metadata sidecar is absent",
    )

    verify = commands.add_parser("verify", help="validate backup metadata and SQLite integrity")
    verify.add_argument("--source", type=Path, required=True, help="backup SQLite database")

    maintain = commands.add_parser(
        "maintain", help="create a verified backup and prune old generations"
    )
    maintain.add_argument("--source", type=Path, required=True, help="source SQLite database")
    maintain.add_argument("--destination", type=Path, required=True, help="backup directory")
    maintain.add_argument("--daily", type=int, default=7, help="daily generations to retain")
    maintain.add_argument("--weekly", type=int, default=4, help="weekly generations to retain")
    maintain.add_argument("--dry-run", action="store_true", help="report changes without writing or deleting")

    offsite = commands.add_parser(
        "offsite-status", help="record the result of off-site backup verification"
    )
    offsite.add_argument("--source", type=Path, required=True, help="source SQLite database")
    offsite.add_argument("--destination", type=Path, required=True, help="local backup path")
    offsite.add_argument("--success", action="store_true", help="mark off-site verification successful")
    offsite.add_argument("--failure", action="store_true", help="mark off-site verification failed")
    offsite.add_argument("--error", default=None, help="failure reason")
    offsite.add_argument("--offsite-host", required=True, help="off-site SSH host")
    offsite.add_argument("--offsite-path", required=True, help="off-site backup directory")
    offsite.add_argument("--backup-sha256", default=None, help="verified plaintext backup digest")
    offsite.add_argument("--backup-size-bytes", type=int, default=None, help="verified plaintext backup size")
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
            result = restore_database(
                args.source,
                args.destination,
                allow_existing=args.allow_existing,
                allow_legacy_without_metadata=args.allow_legacy_without_metadata,
            )
        elif args.command == "verify":
            metadata = validate_backup_metadata(args.source)
            integrity_check(args.source)
            result = {
                "backup": str(args.source),
                "sha256": metadata["sha256"],
                "size_bytes": metadata["size_bytes"],
                "verified": True,
            }
        elif args.command == "maintain":
            result = maintain_backups(
                args.source,
                args.destination,
                daily_generations=args.daily,
                weekly_generations=args.weekly,
                dry_run=args.dry_run,
            )
        else:
            if args.success == args.failure:
                _parser().error("choose exactly one of --success or --failure")
            record_backup_status(
                args.source,
                args.destination,
                success=args.success,
                error=args.error,
                metadata={
                    "offsite_host": args.offsite_host,
                    "offsite_path": args.offsite_path,
                    "sha256": args.backup_sha256,
                    "size_bytes": args.backup_size_bytes,
                },
            )
            result = {
                "destination": str(args.destination),
                "offsite_host": args.offsite_host,
                "status": "success" if args.success else "failure",
            }
    except BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if isinstance(result, dict):
        print(json.dumps(result, sort_keys=True))
    elif result is not None:
        print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
