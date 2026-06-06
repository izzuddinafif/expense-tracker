# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal expense tracking Telegram bot. Users send receipt photos or text descriptions → LLM extracts expense data → logs to Notion. Also auto-logs bank transactions by polling Gmail IMAP for bank notification emails. Supports income logging and budget/net worth queries.

## Stack

- **aiogram 3.x** — async Telegram bot framework
- **OpenAI SDK** → routes through **OpenRouter** (configurable models)
- **httpx** — async Notion API client (with retry on transport errors, 429, 5xx)
- **pydantic v2** — typed LLM output validation
- **aiosqlite** — SQLite persistence for pending expenses, conversation history, processed emails
- **imaplib** — Gmail IMAP polling for bank emails
- **Python 3.12+** (uses `X | None` union syntax)

## Running

```bash
pip install -r requirements.txt
python main.py
```

Docker:
```bash
docker compose up -d
```

No build step. No tests. Single-process async app.

## Architecture

All code lives at the repo root. Flat module structure:

```
main.py          # Entry point. aiogram handlers + startup. Owns NotionCache ref.
config.py        # Single Config dataclass loaded from env vars + hardcoded Notion DSIDs.
models.py        # Pydantic models (ExpenseEntry, IncomeEntry, QueryIntent, EmailTransaction),
                 # NotionCache (fuzzy-matched relation lookups).
db.py            # Database class (aiosqlite). Persistent state: processed emails,
                 # pending expenses/income, debit card queue, conversation history.
                 # Auto-migrates from legacy processed_emails.json on first run.
agent.py         # LLM calls via OpenRouter: receipt OCR, text extraction, income
                 # extraction, intent detection, query answering, bank email parsing.
                 # System prompts are module-level constants. Includes retry logic
                 # (3 attempts with exponential backoff) and JSON fix-up retry.
notion.py        # NotionClient: query databases, load relation caches, create expense/
                 # income pages, fetch expenses by owner prefix, fetch assets/budgets.
email_watcher.py # Background IMAP polling task. Parses Mandiri/Jago/BSI bank emails.
                 # Auto-logs known expenses. Jago debit card (no merchant) triggers
                 # Telegram follow-up or matches recurring.json.
```

### Data Flow

1. **Receipt photo** → `handle_photo` → `Agent.extract_from_image` (vision model) → `ExpenseEntry` → stored in SQLite → inline keyboard confirmation → `NotionClient.log_expense`
2. **Text input** → `Agent.detect_intent` (classifies as query/log_text/log_income/unknown) → routes to one of:
   - `extract_from_text` → same confirmation flow as photo
   - `extract_income_from_text` → income confirmation flow → `NotionClient.log_income`
   - `answer_query` (fetches user expenses + assets from Notion, uses conversation history from SQLite)
3. **Bank email** → `EmailWatcher._imap_fetch` (IMAP) → `Agent.parse_bank_email` → `EmailTransaction` → auto-log to Notion or Telegram follow-up for Jago debit card (queued in SQLite `pending_debit_queue`)

### Key Design Patterns

- **SQLite persistence**: `db.py` replaces in-memory dicts. Tables: `processed_emails`, `pending_expenses`, `pending_income`, `pending_email_expenses`, `pending_debit_queue`, `conversation_history`. Auto-migrates from legacy `processed_emails.json`.
- **Owner prefix**: Expenses/income stored in Notion as `"[OwnerName] description"` in the title field. Filtering done by string matching on `[Owner]`.
- **Fuzzy matching**: `NotionCache._fuzzy_match` does case-insensitive exact match first, then picks the longest partial match.
- **Confirmation flow**: Both photo and text expenses go through an inline keyboard (Simpan / Batal) before writing to Notion. Income has its own parallel flow.
- **LLM structured output**: All LLM responses parsed as raw JSON from chat completions (no function calling). Prompts include available subcategories/accounts lists. Agent._call_json includes a fix-JSON retry: on parse failure, sends the broken output back and asks the model to fix it.
- **Recurring expenses**: Loaded from "Recurring Payment" Notion database (Active entries only). Jago debit card transactions matching a known amount exactly are auto-logged without asking.
- **Jago debit queue**: Jago debit card emails have no merchant name. They queue in `pending_debit_queue` and are presented one-by-one to the user for description input.
- **Conversation memory**: Query intent uses SQLite-backed conversation history (last 20 messages per user) for follow-up questions.
- **Email watcher auto-restart**: `main.py` wraps the watcher task with `_watch_over` that catches crashes and restarts after 10s.

### External Dependencies

- **Notion**: Uses a specific template ("Budget & Expense Tracker (IDR)") with hardcoded database IDs in `config.py`. Databases: expenses, subcategories, accounts, months, years, recurring payments, assets, income, income subcategories, budget. All relation-based.
- **OpenRouter**: Any model that supports vision + structured JSON output. Configurable via `VISION_MODEL` and `QUERY_MODEL` env vars (defaults to `openrouter/free`).
- **Gmail IMAP**: Requires App Password (not regular password). Polls every 5 min by default. Monitors 3 bank senders: Mandiri, Jago, BSI/BYOND.

## Bot Commands

- `/start` — Welcome message with usage instructions
- `/help` — Detailed help text
- `/networth` — Show asset summary from Notion Assets database
- `/budget` — Show budget status with spent/total per category (resolves subcategory names from cache)
- `/refresh` — Reload Notion relation caches

## Configuration

Copy `.env.example` to `.env` and fill in: `TELEGRAM_TOKEN`, `OPENROUTER_API_KEY`, `NOTION_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.

Optional env vars: `VISION_MODEL`, `QUERY_MODEL`, `EMAIL_POLL_INTERVAL`, `EMAIL_OWNER`, `DB_PATH`.

User ID mapping is hardcoded in `config.py` `load_config()` — not env-var driven.

## Important Conventions

- UI language is **Indonesian** (all bot responses, button labels, error messages).
- All currency amounts in **IDR** (Indonesian Rupiah), no decimals needed.
- Dates in **ISO format** (YYYY-MM-DD).
- Bank email parsing handles Indonesian-language email bodies (Indonesian month abbreviations, IDR amount formatting with periods as thousands separators).
- SQLite database stored at `data/expense_tracker.db` (persisted via Docker volume).
