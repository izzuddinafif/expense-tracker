import os
from dataclasses import dataclass, field

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

    # Telegram user ID → owner name
    users: dict[int, str]

    # Gmail IMAP (for bank email watcher)
    gmail_address: str
    gmail_app_password: str  # Google App Password (not your login password)
    email_poll_interval: int = 300  # seconds between inbox checks (default 5 min)


def load_config() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_base_url="https://openrouter.ai/api/v1",
        notion_token=os.environ["NOTION_TOKEN"],
        vision_model=os.getenv("VISION_MODEL", "openrouter/free"),
        query_model=os.getenv("QUERY_MODEL", "openrouter/free"),
        # Notion DSIDs (from template — "Afif" education workspace)
        expenses_ds="ff8c2adf-8454-8389-a223-07123b6a34df",
        subcategories_ds="c68c2adf-8454-82ff-8e2e-87c05e2a7975",
        accounts_ds="9cfc2adf-8454-836a-b9a8-07bf45eb1de9",
        months_ds="a43c2adf-8454-82e2-a361-87fd09bfb425",
        years_ds="d9fc2adf-8454-8228-b2b2-076d532572b9",
        recurring_ds="409c2adf-8454-83fd-997e-079ec8c04d73",
        # Add your Telegram user IDs here
        users={
            981749333: "Afif",  # replace with your real Telegram ID
            987654321: "Friend",  # replace with your friend's Telegram ID
        },
        # Gmail IMAP credentials
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        email_poll_interval=int(os.getenv("EMAIL_POLL_INTERVAL", "300")),
    )
