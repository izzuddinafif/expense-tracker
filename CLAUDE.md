# Repository Working Notes

This is a single-user, local-first expense tracker. Python 3.12 runs Telegram
and Gmail capture, an Android API, an authoritative SQLite ledger, and an
idempotent Notion outbox. The sideloaded Kotlin/Compose Android client provides
the primary operational UI, notification capture, Room caching, and WorkManager
sync.

Read `AGENTS.md` for contributor rules and `PRODUCTION_ROADMAP.md` for current
architecture and milestone status.

## Commands

```bash
uv sync --locked
python main.py
.venv/bin/pytest tests/test_api.py -q
cd android
./gradlew --no-daemon --max-workers=1 :app:testDebugUnitTest
```

Always run focused pytest files or `-k` selections; never launch the entire
Python suite indiscriminately. Keep Gradle at one worker on this server.

## Architecture

- `main.py` composes lifecycle, Telegram handlers, workers, and the API.
- `api.py` exposes authenticated `/api/v1` Android endpoints.
- `db.py` owns migrations, ledger state, events, outbox, and workflow data.
- `reporting.py`, `local_query.py`, and `local_budgets.py` read SQLite.
- `notion.py` is an external projection client; Notion is not authoritative.
- `email_watcher.py` authenticates and parses allowlisted bank mail.
- `android/` contains Compose UI, notification ingestion, Room, and sync.

Confirmed writes must atomically update SQLite, append their audit event, and
queue any external projection. IDR values are integer rupiah and dates are
strict `YYYY-MM-DD`. External services must be faked in tests.

## Configuration and Operations

Configuration comes from `.env`; users are declared through `TELEGRAM_USERS`.
Never commit tokens, mail passwords, databases, logs, or backups. `uv.lock` is
canonical; regenerate `requirements.txt` from it rather than editing the export.
Use `scripts.sqlite_backup` and follow `docs/BACKUP_OPERATIONS.md` for retention,
restore drills, and off-host copies.
