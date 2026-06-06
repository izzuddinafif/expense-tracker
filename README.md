# Finance Bot

Telegram bot for personal expense tracking. Logs receipts to Notion via OCR + LLM. Auto-logs bank transactions from Gmail.

## Stack
- `aiogram` — Telegram bot framework
- `openai` SDK → OpenRouter (Gemini 2.0 Flash by default)
- `httpx` — Notion API calls
- `pydantic` — typed LLM output validation

## Setup

### 1. Notion Integration
1. Go to https://www.notion.so/my-integrations → New integration
2. Copy the token → `NOTION_TOKEN`
3. Open your "Budget & Expense Tracker (IDR)" workspace
4. Share each database with your integration (top-right → Connect to → your integration)

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

### 5. User IDs
Get your Telegram user ID by messaging @userinfobot.
Update `config.py`:
```python
users={
    YOUR_ID: "Afif",
    FRIEND_ID: "Friend",
}
```

### 6. Run
```bash
cp .env.example .env
# fill in .env

pip install -r requirements.txt
python main.py
```

## Usage
- 📸 Send a receipt photo → extracted + confirmation prompt → reply `yes` to log
- 💬 Send text like `"Grab 45k GoPay"` → same flow
- ❓ Ask `"How much did I spend on food this month?"` → LLM answers from your Notion data
- `/refresh` → reload subcategories/accounts from Notion

### Bank Email Auto-Logging
The bot polls Gmail IMAP every 5 minutes for transaction notifications from:
- **Mandiri** (Livin') — QRIS payments, transfers
- **Jago Syariah** — QRIS payments, transfers, debit card
- **BSI/BYOND** — QRIS payments, transfers

Bank emails are parsed by the LLM and auto-logged to Notion. Jago debit card transactions (which don't include merchant names) trigger a Telegram follow-up asking what was purchased, unless the amount matches a known recurring expense.

### Recurring Expenses
Edit `recurring.json` to define known recurring amounts. Jago debit card transactions matching these amounts are auto-logged without asking:
```json
[
  {"amount": 54990, "description": "Spotify Premium", "subcategory": "Entertainment", "account": "Jago"},
  {"amount": 29000, "description": "YouTube Premium", "subcategory": "Entertainment", "account": "Jago"}
]
```

## Project Structure
```
finance-bot/
├── main.py           # bot handlers + startup
├── config.py         # config + user mapping
├── models.py         # pydantic models + app state
├── notion.py         # notion API client
├── agent.py          # LLM vision + query logic
├── email_watcher.py  # Gmail IMAP polling + bank email processing
├── recurring.json    # known recurring expense definitions
├── requirements.txt
└── .env.example
```
