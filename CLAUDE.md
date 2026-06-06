# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Personal expense tracking Telegram bot. Users send receipt photos or text descriptions → LLM extracts expense data → logs to Notion. Also auto-logs bank transactions by polling Gmail IMAP for bank notification emails.

## Stack

- **aiogram 3.x** — async Telegram bot framework
- **OpenAI SDK** → routes through **OpenRouter** (Gemini 2.0 Flash default)
- **httpx** — async Notion API client
- **pydantic v2** — typed LLM output validation
- **imaplib** — Gmail IMAP polling for bank emails

## Running

```bash
pip install -r requirements.txt
python main.py
```

No build step. No tests. Single-process async app.

## Architecture

All code lives at the repo root (no src/ directory despite what README says). Flat module structure:

```
main.py          # Entry point. aiogram handlers + startup. Owns in-memory state.
config.py        # Single Config dataclass loaded from env vars + hardcoded Notion DSIDs.
models.py        # Pydantic models (ExpenseEntry, QueryIntent, EmailTransaction),
                 # NotionCache (fuzzy-matched relation lookups), ConversationState.
agent.py         # LLM calls via OpenRouter: receipt OCR, text extraction, intent
                 # detection, query answering, bank email parsing. System prompts
                 # are module-level constants.
notion.py        # NotionClient: query databases, load relation caches, create expense
                 # pages, fetch expenses by owner prefix "[OwnerName]".
email_watcher.py # Background IMAP polling task. Parses Mandiri/Jago/BSI bank emails.
                 # Auto-logs known expenses. Jago debit card (no merchant) triggers
                 # Telegram follow-up or matches recurring.json.
```

### Data Flow

1. **Receipt photo** → `handle_photo` → `Agent.extract_from_image` (vision model) → `ExpenseEntry` → inline keyboard confirmation → `NotionClient.log_expense`
2. **Text input** → `Agent.detect_intent` → either `extract_from_text` → same confirmation flow, or `answer_query` (fetches all user expenses from Notion, passes to LLM)
3. **Bank email** → `EmailWatcher._imap_fetch` (IMAP) → `Agent.parse_bank_email` → `EmailTransaction` → auto-log to Notion or Telegram follow-up for Jago debit card

### Key Design Patterns

- **In-memory state only**: `NotionCache`, `ConversationState` dict, processed email UIDs. Lost on restart. `/refresh` command reloads Notion cache.
- **Owner prefix**: Expenses stored in Notion as `"[OwnerName] description"` in the title field. Filtering done by string matching on `[Owner]`.
- **Fuzzy matching**: `NotionCache._fuzzy_match` does case-insensitive exact then partial match for subcategories/accounts.
- **Confirmation flow**: Both photo and text expenses go through an inline keyboard (Log it / Cancel) before writing to Notion.
- **LLM structured output**: All LLM responses parsed as raw JSON from chat completions (no function calling). Prompts include available subcategories/accounts lists. Models defined as Pydantic classes but validation is `json.loads` + `ModelClass(**data)`.
- **Recurring expenses**: `recurring.json` at project root. Jago debit card transactions matching a known amount exactly are auto-logged without asking.

### External Dependencies

- **Notion**: Uses a specific template ("Budget & Expense Tracker (IDR)") with hardcoded database IDs in `config.py`. Databases: expenses, subcategories, accounts, months, years. All relation-based.
- **OpenRouter**: Any model that supports vision + structured JSON output. Configurable via `VISION_MODEL` and `QUERY_MODEL` env vars.
- **Gmail IMAP**: Requires App Password (not regular password). Polls every 5 min by default. Monitors 3 bank senders: Mandiri, Jago, BSI/BYOND.

## Configuration

Copy `.env.example` to `.env` and fill in: `TELEGRAM_TOKEN`, `OPENROUTER_API_KEY`, `NOTION_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`.

User ID mapping is hardcoded in `config.py` `load_config()` — not env-var driven.

## Important Conventions

- All currency amounts in **IDR** (Indonesian Rupiah), no decimals needed.
- Dates in **ISO format** (YYYY-MM-DD).
- Bank email parsing handles Indonesian-language email bodies (Indonesian month abbreviations, IDR amount formatting with periods as thousands separators).
- The `processed_emails.json` file tracks which emails have been handled (persisted to disk).
