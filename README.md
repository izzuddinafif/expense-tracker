# Finance Bot

Telegram bot for personal expense tracking. Logs receipts to Notion via OCR + LLM.

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

### 4. User IDs
Get your Telegram user ID by messaging @userinfobot.
Update `config.py`:
```python
users={
    YOUR_ID: "Afif",
    FRIEND_ID: "Friend",
}
```

### 5. Run
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

## Project Structure
```
finance-bot/
├── main.py           # bot handlers
├── src/
│   ├── config.py     # config + user mapping
│   ├── models.py     # pydantic models + app state
│   ├── notion.py     # notion API client
│   └── agent.py      # LLM vision + query logic
├── requirements.txt
└── .env.example
```
