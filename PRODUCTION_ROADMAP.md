# Production Roadmap

## Product Direction

Build a reliable, local-first personal finance system with Telegram as the capture interface, SQLite as the source of truth, and Notion as an optional synchronized dashboard. Keep deployment single-process and single-user. Do not add accounts, roles, OAuth, microservices, or cloud infrastructure unless the usage model changes.

## Current State

The app now has an authoritative versioned SQLite ledger, transaction events,
an idempotent Notion outbox, guarded backup/restore tooling, an authenticated
Android API, and a local-first Compose/Room client. Operational state uses a
dedicated SQLite connection, Docker liveness checks the event loop plus
database access, and classified health covers worker freshness, retry streaks,
outbox age, and backups. Conversational expense queries, statistics, search,
exports, and monthly budgets now read the confirmed SQLite ledger. Assets and
net-worth remain explicitly Notion-backed. Remaining structural debt is
concentrated in the large `main.py`, legacy workflow tables, and long-term
retention/pagination. Expanded portfolio/assets and deduplication work is in
progress only: do not mark it complete until its implementation and regression
coverage have landed in this branch.

## Target Architecture

```text
Telegram / Gmail / Android notifications
       |
       v
Input adapters -> application services -> SQLite ledger
                                           |
                                           v
                                      sync_outbox
                                           |
                                           v
                                         Notion
```

Use four clear layers:

- `adapters/`: Telegram handlers, Gmail/IMAP parsing, Notion client, OpenRouter client.
- `services/`: expense capture, confirmation, income, email ingestion, reporting, and synchronization workflows.
- `domain/`: typed entities, money/date rules, categorization, and duplicate policies.
- `storage/`: SQLite repositories, migrations, transactions, and backup utilities.

This is a gradual extraction from existing modules, not a rewrite.

## Android Product Track

The sideloaded Android app becomes the primary operational UI while Telegram remains a secondary capture channel. The app uses Jetpack Compose for presentation, Room for offline cache and ingestion queues, `NotificationListenerService` for allowlisted bank notifications, and WorkManager for durable synchronization.

Backend SQLite remains authoritative. Room stores captured notification evidence, drafts, cached ledger records, and pending sync work; it is not a competing source of truth. Every Android ingestion request carries a stable source fingerprint so the backend can deduplicate it against Telegram and Gmail records.

The first mobile milestone includes:

- notification-access onboarding and bank-app allowlisting;
- a review inbox for captured transactions;
- dashboard, transaction history, and settings shells;
- offline capture into Room;
- an API contract for idempotent ledger submission;
- explicit sync and error states.

## Core SQLite Ledger

Add normalized durable records. Store IDR as integer rupiah, never `REAL`.

### `transactions`

- `id`: UUID text generated locally
- `kind`: `expense`, `income`, or `transfer`
- `status`: `pending`, `confirmed`, `voided`
- `amount_idr`: integer with `CHECK (amount_idr > 0)`
- `occurred_on`: ISO date
- `description`, `merchant`, `category`, `subcategory`, `account`
- `source`: `telegram_text`, `telegram_photo`, `bank_email`, or `manual`
- `source_ref`: nullable stable input identifier
- `created_at`, `updated_at`, `confirmed_at`: UTC timestamps
- `notion_page_id`: nullable
- unique constraint on `(source, source_ref)` when a reference exists

Model transfers explicitly, preferably with a `transfer_details` row containing source account, destination account, and fee. Do not represent a transfer as unrelated expense/income records without a shared ID.

### Supporting tables

- `transaction_events`: append-only audit log (`created`, `extracted`, `edited`, `confirmed`, `sync_failed`, `synced`, `voided`) with JSON metadata.
- `sync_outbox`: operation, entity ID, attempt count, next attempt time, last error, and completion time.
- `ingestion_events`: raw source metadata, payload hash, parse status, and error; raw email bodies can be omitted or retained briefly.
- `categories`, `accounts`, and `recurring_rules`: local cached reference data with stable IDs.
- `schema_migrations`: ordered migration version and applied timestamp.

Keep the existing workflow tables initially. Migrate pending state only after ledger writes are stable.

## Reliable Write Flow

For confirmation, use one SQLite transaction:

1. insert or update the confirmed ledger transaction;
2. append a `confirmed` event;
3. enqueue a Notion `upsert` in `sync_outbox`;
4. clear pending workflow state;
5. commit.

Reply “saved” after the local commit. A background worker synchronizes Notion with exponential backoff and jitter. Notion outages must not block capture. Use the local transaction UUID as the idempotency key stored in a Notion property or title metadata. Sync must support create, update, and archive/void.

## Delivery Phases

### Phase 0 — Baseline and safety

- Update README and `.env.example` to match current configuration.
- Add a `Makefile` or small `scripts/` commands for setup, targeted tests, backup, restore, and database inspection.
- Enable SQLite `foreign_keys`, `busy_timeout`, WAL checkpoint policy, and integrity checks.
- Add timestamped online backups using SQLite's backup API; retain daily and weekly copies.
- Perform and document one restore drill.
- Pin a reproducible dependency set using `uv.lock` as the canonical lock file.

**Done when:** a fresh clone starts from documented commands, backup restoration is proven, and configuration documentation is accurate.

### Phase 1 — Local source of truth

- Add versioned migrations and the ledger/outbox/event tables.
- Introduce repository methods with explicit transaction boundaries.
- Convert new money writes to integer IDR.
- Dual-write confirmed records to SQLite and the outbox while preserving existing Notion behavior behind a feature flag.
- Backfill existing Notion transactions into SQLite with page IDs and deterministic deduplication.

**Done when:** every confirmed transaction is queryable locally and duplicate input cannot create duplicate ledger rows.

### Phase 2 — Resilient Notion synchronization

- Move all Notion creates/updates/deletes behind the outbox worker.
- Add `/syncstatus` and `/syncretry`.
- Reconcile local records against Notion page IDs and report drift.
- Make local save success independent of Notion availability.

**Done when:** a simulated Notion outage queues writes and later synchronizes them exactly once without user intervention.

### Phase 3 — Modularize without rewriting

- Extract configuration and lifecycle first.
- Move expense/income confirmation into services.
- Split Telegram command handlers by feature.
- Wrap external clients behind narrow protocols so tests can use fakes.
- Keep parsing functions pure where possible.

**Done when:** `main.py` is primarily composition/startup and workflows are testable without creating a Telegram dispatcher.

### Phase 4 — Reporting and data quality

- Run `/stats`, `/budget`, `/search`, and exports from SQLite.
- Add correction and void workflows rather than destructive deletion.
- Add rules for merchant normalization and category overrides.
- Add CSV/JSON export and monthly reconciliation summaries.
- Track uncategorized, duplicate-suspected, and sync-failed items.

**Done when:** common finance questions and exports work during a Notion outage.

### Phase 5 — Operational hardening

- Add structured logs with correlation IDs (`transaction_id`, email UID, Telegram update ID).
- Track worker heartbeat, last successful IMAP poll, outbox depth, oldest pending job, and last backup.
- Make health checks verify database access and worker freshness.
- Gracefully stop intake, finish/return in-flight jobs, checkpoint WAL, and close clients.
- Add alert deduplication so one outage produces one actionable Telegram alert.

**Done when:** `/health` identifies the failed dependency and recovery does not require database surgery.

## Testing Strategy

Do not chase a coverage percentage. Protect money and state transitions:

- unit tests for IDR parsing, dates, bank parsers, categorization, and transfer rules;
- repository tests against temporary SQLite databases for constraints and migrations;
- service tests for confirm/edit/cancel/undo and idempotent ingestion;
- outbox tests for retry, restart recovery, duplicate delivery, and poison jobs;
- contract tests using recorded sanitized Notion/OpenRouter responses;
- one small end-to-end happy path with fake external adapters.

Run focused files or `-k` selections locally. Reserve full-suite CI runs for completed features and release candidates.

## Recommended First Milestone

Implement only Phase 0 and the smallest Phase 1 slice:

1. versioned migration runner;
2. `transactions`, `transaction_events`, and `sync_outbox`;
3. integer-IDR domain model;
4. atomic local confirmation plus queued Notion sync;
5. focused recovery and idempotency tests;
6. database backup and restore commands.

Avoid a dashboard for now. Telegram plus Notion already provide sufficient UI; reliability and ownership of the financial history are the highest-value improvements.

## Milestone Status

The initial local-ledger and Android foundation is implemented:

- versioned SQLite ledger, transaction audit events, and Notion outbox;
- local-first Telegram expense/income confirmation;
- authenticated `/api/v1` Android ingestion and transaction endpoints;
- strict notification fingerprint and cross-source bank transaction deduplication;
- Compose dashboard, review inbox, transaction history, and settings;
- Room notification queue and backend ledger projection;
- WorkManager push/pull synchronization;
- BSI BYOND, Livin Mandiri, and Jago sanitized parser fixtures;
- atomic Android notification confirmation and stable per-notification identity;
- notification-to-backend instrumentation E2E through real WorkManager;
- on-device notification-access and recent-capture diagnostics;
- automatic Notion outbox processing with bounded backoff and retry isolation;
- transaction-UUID lookup and canonical repair for idempotent outbox retries;
- single-writer Telegram confirmation flow with durable recurring relation IDs;
- local-first Gmail expenses and deterministic transfer/income/fee components;
- authenticated sync status and manual retry API controls;
- Android sync diagnostics with refresh and manual retry action;
- online SQLite backup and guarded restore CLI with a documented restore drill;
- audited SQLite edit/void mutations with idempotent Notion upsert/archive;
- local-first Android transaction details, correction, and void queue;
- legacy Telegram edit/undo callbacks routed through the canonical ledger;
- read-only SQLite/Notion reconciliation with duplicate, missing, wrong-kind,
  page-ID, unexpected-page, and active-void detection;
- authenticated classified operational health and real `/livez` checks;
- dedicated operational-state connection and supervised core workers;
- atomic verified backups with failure heartbeats, size, and SHA-256 metadata;
- Android periodic sync, bounded poison-operation isolation, and local/server
  diagnostics;
- signed cursor-based Android change feed with full pagination, void
  tombstones, and protection for unsynced local edits;
- live per-transaction unfinished-work checks that prevent a concurrent change
  feed from temporarily clobbering a newly queued local mutation;
- persistent per-email retry/degraded/terminal state with Android inspection
  and manual retry;
- conservative daily/weekly backup retention with dry-run and JSON output;
- SQLite-backed `/stats`, `/search`, and CSV export that remain available
  during Notion outages;
- SQLite-backed conversational expense queries and monthly `/budget` set,
  delete, and report commands;
- authenticated monthly budget API plus an Android Budgets tab with month
  navigation, usage progress, validation, editing, deletion, and retry states;
- versioned SQLite snapshots of the last successful Notion taxonomy/recurring
  cache, used as a fallback by Telegram and Gmail during Notion outages;
- extracted and independently tested Telegram budget command service;
- SQLite-backed recent, keyword, duplicate, and merchant context for Telegram
  and Gmail capture, eliminating live Notion reads from active detection paths;
- tested Android current-month dashboard aggregation and presentation for
  expense, income, net flow, top categories, and recent transactions;
- scalable Android transaction history with lazy date sections, local search,
  and expense/income filters;
- editable review-before-save for uncertain Android notification captures,
  with corrected values committed atomically to Room and the sync outbox;
- scroll- and keyboard-safe Android settings and transaction-detail forms;
- bounded operational diagnostic collections to avoid eager rendering growth;
- honest Android sync-now acknowledgement and accessible live status regions
  for queued sync, review validation, and transaction mutation results;
- validated and normalized Android API settings with explicit save/sync
  feedback;
- state-preserving Android top-level navigation with full-screen detail and
  diagnostics routes;
- coherent calm-dark Android design tokens, semantic financial colors, shared
  card/spacing components, and tabular amount typography;
- offline manual expense/income entry with validated fields, signed local
  amounts, explicit provenance, single-flight submission, and an atomic
  Room/outbox write;
- authoritative manual/notification deduplication that preserves an intentional
  manual row when a matching bank notification arrives later;
- local, recency-ranked manual-entry suggestions with case-insensitive
  deduplication and BSI/Mandiri/Jago/Cash account fallbacks;
- live grouped-IDR feedback and a timezone-safe Material date picker for
  manual entries;
- race-safe offline correction and void of unsent manual entries through
  atomic create-payload compaction and recoverable FIFO worker claims;
- focused manual-income unit and Android 15 emulator E2E coverage through the
  Compose form, Room projection, and pending backend payload;
- Gradle wrapper with resource-capped defaults;
- Android 15 emulator validation, including the new budget navigation state.
- revision-fenced Notion outbox completion so concurrent edits cannot be lost;
- exact source-reference idempotency without cross-source amount/date suppression;
- fail-closed Notion token encryption for new credentials;
- authenticated bank-email sender checks, strict parsed transaction validation,
  and external-model redaction;
- crash-atomic and claim-fenced Android sync finalization with a durable
  incremental feed checkpoint;
- recoverable failed Android operations and bounded sent-operation retention;
- non-destructive notification repost detection that retains both captures and
  flags suspected duplicates for review;
- release-only HTTPS enforcement with debug-LAN HTTP opt-in;
- reproducible `uv.lock`-based container and local dependency installation;
- revision-fenced Android edits and voids, with a server-copy recovery action
  for permanently failed local mutations;
- serialized SQLite writes across Telegram, Gmail, API, backup, and budget
  paths, including concurrency regression coverage;
- conservative notification direction detection that keeps credits, generic
  success templates, and ambiguous captures in review;
- token-aware budget matching with category fallback and collision coverage;
- graceful webhook signal handling and documented off-host backup operations.

Ledger/outbox delivery is idempotent because both live Notion databases now
contain the required rich-text `Transaction ID` property. Ambiguous create
responses are reconciled by UUID lookup and existing pages are repaired with
canonical data. Telegram, Android, and Gmail creation paths use the outbox;
edit/void/undo paths also mutate the canonical ledger and enqueue Notion work.

Physical-device acceptance is complete: the connected S23 received and was
validated against the real BSI BYOND, Livin Mandiri, and Jago notification
flows, with notification access enabled and the current client connected to
the production API. Remaining roadmap work is non-blocking product evolution:

1. continue extracting handlers/services from `main.py` and validate budget
   totals/categories against real usage data;
2. keep Notion-backed assets as the external source while local assets provide
   explicit valuations and liabilities; duplicate names are qualified as
   incomplete until the user links or removes one;
3. keep extracting handlers/services from `main.py` and extend real-device
   acceptance for additional bank/email combinations.

The API 35 emulator suite is currently green at 23/23, the Android JVM suite
at 71/71, and focused portfolio/dedup/backend regressions at 39/39. The
HTTPS-enforcing
release variant has been assembled locally, but the artifact in this workspace
is signed with the Android debug key. The external Ledgerly release-keystore
password is not available here, so signing and stable-update distribution are
gated: do not claim a stable release until the external keystore is supplied
and `scripts/build_ledgerly_release.sh` verifies a non-debug certificate,
versionCode, and SHA-256. The Coolify Ledgerly application must be verified
after deployment with `scripts/verify_ledgerly_production.sh`; historical
`/livez` success is not evidence of current production health.

### Release and implementation handoff gates

- [x] Fail-closed release build helper and non-secret SG verification helper
  are documented in this repository.
- [ ] The operator supplies the existing external release-keystore password
  and builds an APK that verifies with a non-debug certificate.
- [ ] The signed APK's SHA-256 and versionCode are recorded before sideload
  distribution; no debug-signed APK is treated as a stable update.
- [x] Portfolio/assets and notification/email deduplication implementation,
  including null/partial data, account aliases, self-transfer reclassification,
  conflict quarantine, and regression coverage, is landed in this branch.
- [ ] An SG post-deploy run of the production verification helper passes and
  is retained as operational evidence.

Physical BSI/Mandiri/Jago notification delivery is now accepted based on the
real-device validation above; the parser and notification-queue paths remain
covered by sanitized fixtures and emulator tests for regression protection.
