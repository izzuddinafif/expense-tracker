# Expense Tracker

Local-first personal expense tracker. The Python service captures Telegram and
Gmail activity, stores confirmed records in an authoritative SQLite ledger, and
synchronizes them to Notion. The sideloaded Android client adds a full mobile UI
and bank-notification capture.

## Stack
- `aiogram` — Telegram bot framework
- `openai` SDK → OpenRouter (Gemini 2.0 Flash by default)
- `httpx` — Notion API calls
- `pydantic` — typed LLM output validation
- `aiosqlite` — authoritative ledger, audit events, and sync outbox
- `aiohttp` — Telegram webhook and versioned Android API
- `Jetpack Compose`, Room, WorkManager — Android UI, offline queue, and sync

## Setup

### 1. Notion Integration
1. Go to https://www.notion.so/my-integrations → New integration
2. Copy the token → `NOTION_TOKEN`
3. Open your "Budget & Expense Tracker (IDR)" workspace
4. Share each database with your integration (top-right → Connect to → your integration)
5. Add a rich-text property named exactly `Transaction ID` to both the
   **Expenses** and **Income** databases. New ledger writes remain queued if
   this property is missing; the worker never falls back to duplicate-prone
   creation.

### 2. Telegram Bot
1. Talk to @BotFather → `/newbot`
2. Copy token → `TELEGRAM_TOKEN`

### 3. OpenRouter
1. https://openrouter.ai → API Keys
2. Copy key → `OPENROUTER_API_KEY`

### 4. Gmail (for bank email auto-logging)
1. Enable 2FA on your Google account
2. Generate an App Password at https://myaccount.google.com/apppasswords
3. Set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD` in `.env`

### 5. User and Android API

Get your Telegram user ID from `@userinfobot`. Set `TELEGRAM_USERS` using
`id:name` entries. To enable the personal Android API, generate a random
`API_TOKEN`, set `API_USER_ID` to that Telegram ID, and configure the same base
URL and token in the app.

### 6. Run
```bash
cp .env.example .env
# fill in .env

uv sync --locked
python main.py
```

`uv.lock` is the canonical dependency lock. `requirements.txt` is a generated,
hash-pinned runtime export for environments that cannot use `uv`; do not edit
it by hand.

### SQLite backup and restore

Create an online backup while the service is running. The command writes to a
temporary file, verifies SQLite integrity, then atomically publishes the
timestamped backup with size and SHA-256 metadata:

```bash
python -m scripts.sqlite_backup backup \
  --source data/expense_tracker.db --destination data/backups
```

Preview daily/weekly retention, then run the same command without `--dry-run`.
This command only removes exact timestamped backups belonging to the specified
source; unrelated files and symlinks are ignored:

```bash
python -m scripts.sqlite_backup maintain \
  --source data/expense_tracker.db --destination data/backups \
  --daily 7 --weekly 4 --dry-run
```

Run `maintain` once daily from the deployment server's scheduler after
verifying the destination and dry-run output. Copy completed backups and their
`.json` metadata to a different machine or storage provider; a backup on the
same server does not protect against disk or host loss. See
[`docs/BACKUP_OPERATIONS.md`](docs/BACKUP_OPERATIONS.md) for a systemd example
and an off-host verification checklist.

Restore into a new path first, then run an integrity check by opening it with
the application. Restoring over an existing database requires
`--allow-existing`; stop the service first and retain the previous database
until the restore is verified.

```bash
python -m scripts.sqlite_backup restore \
  --source data/backups/expense_tracker-TIMESTAMP.db \
  --destination data/expense_tracker-restored.db
```

The API is mounted at `/api/v1` when `API_TOKEN` and `API_USER_ID` are set. It
runs alongside either webhook or polling mode.

Android release builds require an HTTPS API URL. Debug builds may use plain
HTTP for trusted-LAN development only; never expose the API token over an
untrusted network.

The unauthenticated `GET /livez` endpoint exposes only local process/database
liveness and is used by Docker. Dependency failures do not create restart
loops.

For webhook mode, set `WEBHOOK_DOMAIN`, `WEBHOOK_PATH`, and a random
`WEBHOOK_SECRET`. The application serves plain HTTP on `PORT`; Coolify or
another reverse proxy must terminate HTTPS with a publicly trusted certificate
for the exact webhook hostname. Telegram will reject a default/self-signed
proxy certificate.

Authenticated Android sync controls are available at:

- `GET /api/v1/sync` — pending/failed outbox summary and recent errors.
- `POST /api/v1/sync/retry` — make failed jobs immediately eligible for retry.
- `GET /api/v1/ops/health` — classified worker, backup, and outbox freshness.
- `GET /api/v1/reconciliation` — read-only SQLite-versus-Notion drift report.
- `GET /api/v1/transactions/changes` — signed cursor-based confirmed/voided
  change feed used by Android.
- `GET /api/v1/email-failures` — auditable per-email retry/terminal failures.
- `POST /api/v1/email-failures/{uid}/retry` — re-enable one failed Gmail UID.
- `GET /api/v1/budgets?month=YYYY-MM` — read authoritative monthly budget use.
- `PUT /api/v1/budgets` — idempotently create or update a monthly budget.
- `DELETE /api/v1/budgets?month=YYYY-MM&category=...` — remove a budget.

## Usage
- 📸 Send a receipt photo → extracted + confirmation prompt → reply `yes` to log
- 💬 Send text like `"Grab 45k GoPay"` → same flow
- `/stats` → current-month totals, trend, projection, and categories from SQLite.
- `/search merchant` → search confirmed local expenses without contacting Notion.
- `/export thismonth|YYYY-MM|all` → export confirmed ledger rows as UTF-8 CSV.
- ❓ Ask `"How much did I spend on food this month?"` → conversational query
  flow answered from confirmed expense rows in the local SQLite ledger.
- `/networth` → show the Notion-backed Assets summary (when configured).
- `/budget` → show the current month's local SQLite budgets.
  Use `/budget YYYY-MM` for another month, `/budget set <amount> <category>`,
  `/budget set YYYY-MM <amount> <category>`, and the equivalent
  `/budget delete [YYYY-MM] <category>` commands to manage them.
- `/refresh` → reload subcategories/accounts from Notion

### Bank Email Auto-Logging
The bot polls Gmail IMAP every 5 minutes for transaction notifications from:
- **Mandiri** (Livin') — QRIS payments, transfers
- **Jago Syariah** — QRIS payments, transfers, debit card
- **BSI/BYOND** — QRIS payments, transfers

Bank emails are parsed by the LLM and committed to SQLite before being
synchronized by the Notion outbox worker. Self-transfers create deterministic
outgoing, incoming, and optional fee components. Jago debit card transactions
(which don't include merchant names) trigger a durable Telegram follow-up
asking what was purchased, unless the amount matches a known recurring
expense. Per-UID processing failures remain retryable for two attempts, become
degraded at three, and become terminal at eight attempts or after 24 hours.
Terminal items remain visible in Android Diagnostics for manual retry.
The last successful Notion taxonomy cache (accounts, categories, relation
URLs, and recurring rules) is also snapshotted in SQLite, so classification
keeps its known reference data during a temporary Notion outage. Existing
snapshots load immediately at startup; use `/refresh` after editing taxonomy or
recurring definitions in Notion.

### Android Client

Open [`android/`](android/) in Android Studio with SDK 35. See
[`android/README.md`](android/README.md) for build, sideloading, notification
access, sync configuration, and the local-first transaction editor. Confirmed
transactions can be corrected or voided offline; Room queues the mutation and
WorkManager reconciles it with SQLite and Notion when connectivity returns.
Cash, missed, and income transactions can also be entered manually from the
History tab and are queued through the same authoritative backend path.
The app installs a 15-minute periodic sync, isolates poison operations after
bounded retries, and shows both server and device outbox health in Diagnostics.

## Project Structure
```
expense-tracker/
├── main.py           # bot handlers + startup
├── config.py         # config + user mapping
├── api.py            # authenticated Android API
├── db.py             # SQLite ledger, workflow state, audit, outbox
├── operations.py     # operational-health thresholds and classification
├── reconciliation.py # read-only SQLite/Notion drift reporting
├── reporting.py      # SQLite search, monthly summaries, and CSV export
├── reporting_views.py # pure Telegram report formatting
├── local_budgets.py  # SQLite monthly budget definitions and usage
├── budget_commands.py # parsed/testable Telegram budget workflow
├── local_query.py    # Notion-independent conversational query service
├── reference_store.py # last-known-good Notion taxonomy snapshots
├── models.py         # pydantic models + app state
├── notion.py         # notion API client
├── agent.py          # LLM vision + query logic
├── email_watcher.py  # Gmail IMAP polling + bank email processing
├── android/          # Kotlin/Compose mobile client
├── tests/            # focused pytest coverage
├── requirements.txt
└── .env.example
```
