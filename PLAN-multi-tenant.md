# Multi-Tenant Support Plan

## Goal
Allow any Telegram user to connect their own Notion workspace, without hardcoded IDs or tokens in config.

---

## Approach: Manual Token + Auto-Discovery

Skip OAuth for now (requires public HTTPS server + Notion app registration). Instead:
1. User creates a Notion internal integration and pastes the token into the bot.
2. Bot auto-discovers all database IDs by searching the user's workspace for databases matching known names.
3. Per-user config stored in SQLite.

OAuth can be layered on later without changing the data model.

---

## Phase 1 — Per-User Storage in SQLite

### 1.1 New table: `users`

Add to `db.py` `_init()`:

```sql
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    owner_name    TEXT NOT NULL,
    notion_token  TEXT NOT NULL,
    -- discovered database IDs (NULL until setup completes)
    expenses_ds           TEXT,
    subcategories_ds      TEXT,
    accounts_ds           TEXT,
    months_ds             TEXT,
    years_ds              TEXT,
    recurring_ds          TEXT,
    assets_ds             TEXT,
    income_ds             TEXT,
    income_subcategories_ds TEXT,
    income_months_ds      TEXT,
    income_years_ds       TEXT,
    budget_ds             TEXT,
    categories_ds         TEXT,
    -- setup state
    setup_step    TEXT DEFAULT 'start',  -- 'start' | 'await_name' | 'await_token' | 'discovering' | 'done'
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
```

### 1.2 New `Database` methods

- `get_user(telegram_id) -> UserRecord | None`
- `upsert_user(telegram_id, **fields) -> None`
- `set_user_setup_step(telegram_id, step) -> None`
- `is_setup_complete(telegram_id) -> bool` — checks all `_ds` fields are non-null

### 1.3 `UserRecord` dataclass in `models.py`

```python
@dataclass
class UserRecord:
    telegram_id: int
    owner_name: str
    notion_token: str
    expenses_ds: str | None
    subcategories_ds: str | None
    accounts_ds: str | None
    months_ds: str | None
    years_ds: str | None
    recurring_ds: str | None
    assets_ds: str | None
    income_ds: str | None
    income_subcategories_ds: str | None
    income_months_ds: str | None
    income_years_ds: str | None
    budget_ds: str | None
    categories_ds: str | None
    setup_step: str
```

---

## Phase 2 — Per-User NotionClient and NotionCache

### 2.1 Decouple NotionClient from Config

`NotionClient.__init__` currently takes `Config`. Change it to accept individual parameters:

```python
class NotionClient:
    def __init__(self, notion_token: str, db_ids: dict[str, str]) -> None:
        ...
```

Where `db_ids` is a dict like `{"expenses_ds": "xxx", "subcategories_ds": "yyy", ...}`.

Keep factory helpers:
```python
@classmethod
def from_config(cls, config: Config) -> "NotionClient":
    ...

@classmethod
def from_user(cls, user: UserRecord) -> "NotionClient":
    ...
```

### 2.2 Per-user cache and client in main.py

Replace the single `cache: NotionCache` and `notion: NotionClient` globals with per-user dicts:

```python
user_caches: dict[int, NotionCache] = {}
user_notions: dict[int, NotionClient] = {}
```

Add helper:
```python
async def get_user_notion(user_id: int) -> tuple[NotionClient, NotionCache] | None:
    """Return (NotionClient, NotionCache) for user, or None if not set up."""
    ...
```

`/refresh` command refreshes only the calling user's cache.

---

## Phase 3 — Database Auto-Discovery

### 3.1 Known database names (from the Notion template)

```python
DB_NAME_MAP = {
    "expenses_ds":              "Expenses",
    "subcategories_ds":         "Expenses Sub-categories",
    "accounts_ds":              "Accounts",
    "months_ds":                "Month",
    "years_ds":                 "Year",
    "recurring_ds":             "Recurring Payment",
    "assets_ds":                "Assets",
    "income_ds":                "Income",
    "income_subcategories_ds":  "Income Sub-categories",
    "income_months_ds":         "Month",         # same name as months_ds — resolve by schema
    "income_years_ds":          "Year",          # same as years_ds
    "budget_ds":                "Budget",
    "categories_ds":            "Expenses Categories",
}
```

Note: `months_ds` and `income_months_ds` share the name "Month". Since the template uses the same Month/Year DBs for both, they're the same IDs — just store one and reuse.

### 3.2 Discovery function in `notion.py`

```python
async def discover_databases(self) -> dict[str, str]:
    """
    Search the user's Notion workspace for all databases by name.
    Returns {field_name: database_id} for every DB found.
    Raises RuntimeError listing any DBs that couldn't be found.
    """
    # Use POST /v1/search with filter {"value": "database", "property": "object"}
    # Paginate through all results
    # Match by title against DB_NAME_MAP values
    # Return mapping
```

### 3.3 Setup flow state machine

Steps:
1. `start` → `/start` or `/setup` triggers, bot says "Welcome! Let's connect your Notion workspace."
2. `await_name` → bot asks "What should I call you?" → user replies → save `owner_name`, advance to `await_token`
3. `await_token` → bot asks for Notion integration token with instructions → user pastes token → save `notion_token`, advance to `discovering`
4. `discovering` → bot says "Searching your Notion workspace..." → call `discover_databases()` → on success save all IDs, set step to `done` → on failure tell user which DBs weren't found and ask them to share the page with the integration

The `handle_text` message handler checks `setup_step` before doing anything else. If not `done`, it routes to the setup flow instead of expense logic.

---

## Phase 4 — Gate All Handlers Behind Setup Check ✅ DONE

All handlers now use `get_user_notion()` or `db.get_user()` to check setup state and get per-user Notion client/cache:
- `/start`, `/help` — check setup, redirect to `/setup` if incomplete
- `/networth`, `/budget`, `/refresh`, `/search`, `/stats` — use per-user Notion
- `handle_photo` — uses per-user Notion
- `handle_text` — uses per-user Notion for query/log_text/log_income
- All callback handlers (`confirm:`, `cancel:`, `income_confirm:`, `income_cancel:`, `edit_cat:*`, `edit_subcat_pick:*`, `subcat_pick:*`) — use per-user Notion
- `_process_next_photo` — uses per-user Notion
- `alert_owner` — loads users from DB instead of config
- Removed `get_owner()` function and `all_user_ids` config-based variable
- `format_entry`/`format_income_entry` — accept optional cache param, fallback to global

---

## Phase 5 — Remove Hardcoded User/DB Config

### 5.1 `config.py` changes

Remove from `Config`:
- `users: dict[int, str]`
- All `*_ds` database ID fields
- `notion_token`

Keep in `Config` (bot-level, not user-level):
- `telegram_token`
- `openrouter_api_key`, `openrouter_base_url`
- `vision_model`, `query_model`
- `gmail_address`, `gmail_app_password`, `email_poll_interval`, `email_owner`
- `db_path`

### 5.2 Agent changes

`Agent` currently takes `Config` only to read model names and API key. No change needed — it has no user-specific data.

---

## Phase 6 — Email Watcher Multi-User

`EmailWatcher` is currently hardcoded to one owner. Since email is one inbox, it stays single-user, but the owner should now come from the DB rather than config:

- Remove `owner_name` and `owner_telegram_id` constructor params
- On startup, look up `EMAIL_OWNER` name in `users` table → get `telegram_id` and `UserRecord`
- Use `from_user(record)` to build that user's `NotionClient`

If `EMAIL_OWNER` user hasn't completed setup, log a warning and skip email watching until they do.

---

## File Change Summary

| File | Change | Status |
|------|--------|--------|
| `db.py` | Add `users` table, `UserRecord` dataclass methods, `get_all_users()` | ✅ Done |
| `models.py` | Add `UserRecord` dataclass | ✅ Done |
| `notion.py` | `NotionClient` takes token + db_ids directly; add `discover_databases()`; add `DB_NAME_MAP`; `from_config()`/`from_user()` factories | ✅ Done |
| `main.py` | Setup flow, per-user Notion/Cache, all handlers gated, `_process_next_photo` per-user, `alert_owner` from DB | ✅ Done |
| `config.py` | Remove `users` dict and all `*_ds` fields, remove `notion_token` | ⏳ Phase 5 |
| `email_watcher.py` | Look up owner from DB instead of constructor params | ⏳ Phase 6 |
| `.env.example` | Remove `NOTION_TOKEN`; add setup instructions | ⏳ Phase 5 |

---

## Migration for Existing User (Afif)

On first run after the change, Afif won't be in the `users` table and will be sent through the setup flow. To avoid this, add a migration in `db.py` `_migrate_*` that reads the old config env vars and pre-populates the `users` table if `NOTION_TOKEN` and `TELEGRAM_USER_ID` are set. Document this in `.env.example`.

Or simpler: just run `/setup` once after deploying.

---

## Out of Scope (for now)

- OAuth — add later without changing the data model (just change how `notion_token` is obtained)
- Per-user Gmail / email watching — stays single-inbox
- Admin commands to list/manage users
