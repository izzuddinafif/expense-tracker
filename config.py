import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    telegram_token: str
    openrouter_api_key: str
    openrouter_base_url: str
    vision_model: str
    query_model: str

    # Gmail IMAP (for bank email watcher)
    gmail_address: str
    gmail_app_password: str  # Google App Password (not your login password)
    email_poll_interval: int = 300  # seconds between inbox checks (default 5 min)

    # Name of the user who owns the Gmail inbox (must match an owner_name in `users` table).
    # The email watcher sends Telegram follow-ups to this user.
    email_owner: str = ""

    # SQLite persistent storage
    db_path: str = "data/expense_tracker.db"

    # Legacy single-tenant fields (deprecated, kept for backward compatibility during transition).
    # New code should use db.get_user() instead.
    notion_token: str = ""
    expenses_ds: str = ""
    subcategories_ds: str = ""
    accounts_ds: str = ""
    months_ds: str = ""
    years_ds: str = ""
    recurring_ds: str = ""
    assets_ds: str = ""
    income_ds: str = ""
    income_subcategories_ds: str = ""
    income_months_ds: str = ""
    income_years_ds: str = ""
    budget_ds: str = ""
    categories_ds: str = ""
    users: dict[int, str] = field(default_factory=dict)


def load_config() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_base_url="https://openrouter.ai/api/v1",
        vision_model=os.getenv("VISION_MODEL", "openrouter/free"),
        query_model=os.getenv("QUERY_MODEL", "openrouter/free"),
        # Gmail IMAP credentials
        gmail_address=os.environ["GMAIL_ADDRESS"],
        gmail_app_password=os.environ["GMAIL_APP_PASSWORD"],
        email_poll_interval=int(os.getenv("EMAIL_POLL_INTERVAL", "300")),
        email_owner=os.getenv("EMAIL_OWNER", "Afif"),
        db_path=os.getenv("DB_PATH", "data/expense_tracker.db"),
        # Legacy single-tenant env vars (optional, for backward compat)
        notion_token=os.getenv("NOTION_TOKEN", ""),
        users=(
            {
                int(item.split(":", 1)[0]): item.split(":", 1)[1]
                for item in os.getenv("TELEGRAM_USERS", "").split(",")
                if ":" in item
            }
            if os.getenv("TELEGRAM_USERS")
            else {}
        ),
    )
