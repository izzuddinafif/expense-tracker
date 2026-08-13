# Ledgerly Android

Native Kotlin/Jetpack Compose client for the expense tracker. It is local-first:
Room stores an offline projection of the authoritative backend ledger plus the
notification review queue. Confirmed or dismissed inbox items update local
state immediately; confirmed transactions are added to an outbox and synced by
a unique, network-constrained WorkManager job.
The app also installs a 15-minute periodic sync so a missed immediate enqueue
does not strand local work. Failed operations are retried independently and
become visible as failed after the bounded retry limit.

## Build and install

Install Android Studio with SDK 35 and JDK 17, open this `android/` directory,
and sync Gradle. Run:

```bash
./gradlew --no-daemon --max-workers=1 \
  -Pkotlin.compiler.execution.strategy=in-process \
  :app:testDebugUnitTest :app:assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

For a local signed release artifact, keep the keystore and passwords outside
the repository and provide them only through the environment:

```bash
export LEDGERLY_SIGNING_STORE_FILE="$HOME/.config/ledgerly/android-release.keystore"
export LEDGERLY_SIGNING_STORE_PASSWORD='use-your-local-secret'
export LEDGERLY_SIGNING_KEY_ALIAS='ledgerly'
export LEDGERLY_SIGNING_KEY_PASSWORD="$LEDGERLY_SIGNING_STORE_PASSWORD"
./gradlew --no-daemon --max-workers=1 \
  -Pkotlin.compiler.execution.strategy=in-process \
  :app:assembleRelease
```

If signing variables are absent, `assembleRelease` remains useful as an
unsigned CI/build verification artifact; distribution builds must set all four.

## Emulator E2E tests

The command-line SDK includes an API 35 AOSP AVD named
`ledgerly_e2e_api35`. Start it headlessly, then run the Compose
instrumentation suite:

```bash
emulator @ledgerly_e2e_api35 -no-window -no-audio -no-boot-anim
./gradlew --no-daemon --max-workers=1 \
  -Pkotlin.compiler.execution.strategy=in-process \
  :app:connectedDebugAndroidTest
```

On low-memory hosts, build `:app:assembleDebugAndroidTest` with the emulator
stopped, then install both APKs and invoke `AndroidJUnitRunner` through `adb`.
If OpenJDK 21's G1 collector is unstable, add
`-Dorg.gradle.jvmargs="-Xmx768m -XX:+UseSerialGC -Dfile.encoding=UTF-8"` to the
Gradle command. This changes only the build JVM.
Use a clean app install when validating WorkManager behavior so jobs retained
from an older build cannot affect the result. Focused instrumentation tests
cover navigation, persisted backend settings, notification parse → Room review
→ WorkManager sync, and manual transaction creation.

Enable **Settings > Notifications > Notification access > Ledgerly** to
capture allowlisted apps. Configure the backend base URL and `API_TOKEN` in the
app's Settings screen. Open **Settings > Notification diagnostics** to verify
notification access and inspect captures from BYOND by BSI, Livin' by Mandiri,
and Jago, local/server outbox state, worker freshness, and the manual
SQLite-versus-Notion reconciliation report. Degraded/terminal Gmail items are
listed there with a manual retry action. Captured notification text stays in `ledgerly.db` until you confirm
or dismiss it. The client deduplicates only an exact stable Android
notification identity; equal notification text can still represent separate
purchases. A same-content notification with a different Android identity is
kept and marked as a possible repost for review, never silently discarded.
The backend then applies its own stable source-fingerprint and strict
bank/date/amount matching.

Canonical refresh uses a signed, cursor-based change feed rather than a
200-row snapshot. WorkManager follows every page, applies server-side voids,
and protects local records that still have pending or failed offline edits.

The **Budgets** tab reads and manages monthly category targets directly through
the authenticated backend API. SQLite remains authoritative; budget definitions
are intentionally not cached or edited through Room, so the screen requires a
reachable server and shows retryable errors when it is offline.

The dashboard derives its totals from the Room projection for the current local
calendar month. It separates expense, income, and net flow, ranks monthly
expense categories, and excludes transactions outside the displayed month.
The **Transactions** tab is also Room-backed and remains usable offline. Its
lazy, date-grouped history presents the most recent 500 records, which can be
narrowed by merchant, description, category, or account and filtered to
expenses or income without loading data from the server. **Sync now** queues
WorkManager and acknowledges that queued state; it does not claim a network
refresh completed synchronously.

Use **Add transaction** in History for cash purchases, missed notifications,
or income. Expense and income entries are committed locally with the correct
signed amount and a stable `manual` source ID, then queued atomically for the
authoritative backend. Submission is single-flight, so a rapid double tap
cannot create two rows. A later matching bank notification reuses the manual
ledger row. Recent local categories and accounts are offered as quick chips,
with BSI, Mandiri, Jago, and Cash available on a fresh ledger. Amount input
shows a live grouped IDR preview, and the Material date picker stores calendar
dates without timezone drift. The entry remains visible offline while
WorkManager retries sync. Before its first upload, the entry can still be
corrected or voided: edits replace the queued create payload, while a void
removes that unsent payload. A short-lived worker claim prevents these actions
from racing an upload already in progress.

Settings accepts absolute HTTPS endpoints, normalizes trailing slashes, and
refuses to persist an invalid URL or blank token. Debug builds may explicitly
use a LAN HTTP endpoint for local development; release builds do not. The
device token is masked until the reveal control is used and stored with an
Android Keystore-backed encrypted preference. Existing plain-text token
preferences are migrated on their first read and then removed. A successful
save explicitly confirms that the initial sync was queued.
Top-level tabs preserve their own state without stacking duplicate
destinations. Transaction details and diagnostics use the full screen and
restore the originating tab when navigating back.

The UI uses a permanent calm-dark ledger theme with complete Material 3 color
roles, rounded shape and typography tokens, tabular financial numerals, and
separate colors for primary actions, positive cashflow, warnings, expenses, and
charts. Shared cards and spacing keep Settings, Budgets, and transaction rows
visually consistent.

For a connected physical device, confirm BYOND's installed package with:

```bash
adb shell pm list packages | grep -Ei 'bankbsi|bsm|byond'
```

The current BYOND package is `co.id.bankbsi.superapp`; legacy BSI Mobile is
`com.bsm.activity2`. BYOND may refuse to open while Android Developer options
are enabled; Ledgerly can still capture an already delivered notification
without opening BYOND, but real-device validation should use sanitized test
notifications where possible. Never paste unsanitized bank notifications, OTPs, balances,
account numbers, or transaction references into fixtures or issue reports.

Notifications whose parser result is uncertain open a review form instead of
being saved immediately. Correct the merchant, amount, date, description,
category, and account there; the reviewed values are committed atomically to
Room and its pending backend operation.
