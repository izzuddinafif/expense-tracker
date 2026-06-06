import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    telegram_token: str
    openrouter_api_key: str
    openrouter_base_url: str
    notion_token: str
    vision_model: str
    query_model: str

    # Notion data source IDs
    expenses_ds: str
    subcategories_ds: str
    accounts_ds: str
    months_ds: str
    years_ds: str
    recurring_ds: str
    assets_ds: str

    # Telegram user ID → owner name
    users: dict[int, str]

    # Gmail IMAP (for bank email watcher)
    gmail_address: str
    gmail_app_password: str  # Google App Password (not your login password)
    email_poll_interval: int = 300  # seconds between inbox checks (default 5 min)

    # SQLite persistent storage
    db_path: str = "data/expense_tracker.db"


def load_config() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_base_url="https://openrouter.ai/api/v1",
        notion_token=os.environ["NOTION_TOKEN"],
        vision_model=os.getenv("VISION_MODEL", "openrouter/free"),
        query_model=os.getenv("QUERY_MODEL", "openrouter/free"),
        # Notion DSIDs (from "Afif" workspace)
        expenses_ds="ddfc2adf-8454-8284-b354-018f7f81c309",
        subcategories_ds="ca4c2adf-8454-83bc-bb02-81fcc02735fe",
        accounts_ds="04bc2adf-8454-820b-ac2f-81a503b7b261",
        months_ds="151c2adf-8454-83f6-b6e3-81f471896e43",
        years_ds="bb6c2adf-8454-82d7-9b50-8136332ea54e",
        recurring_ds="49bc2adf-8454-82fa-a3be-81d6729d0bae",
        assets_ds="86993dea-618a-473a-9b6d-4a271a16a26e",
        # Add your Telegram user IDs here
        users={
            981749333: "Afif",
        },
        # Gmail IMAP credentials
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        email_poll_interval=int(os.getenv("EMAIL_POLL_INTERVAL", "300")),
        db_path=os.getenv("DB_PATH", "data/expense_tracker.db"),
    )
