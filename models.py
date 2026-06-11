import math
from dataclasses import dataclass, field
from pydantic import BaseModel, field_validator


def _validate_amount(cls, v: float) -> float:
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"Amount must be a positive finite number, got {v}")
    return v


# ── LLM output models ────────────────────────────────────────────────────────

class ExpenseEntry(BaseModel):
    description: str
    amount: float
    date: str           # ISO format: YYYY-MM-DD
    subcategory: str    # must match a name in NotionCache.subcategories
    account: str        # must match a name in NotionCache.accounts
    confidence: float   # 0.0–1.0, how confident the model is in the extraction

    _validate_amount = field_validator("amount")(_validate_amount)


class IncomeEntry(BaseModel):
    description: str
    amount: float
    date: str           # ISO format: YYYY-MM-DD
    subcategory: str    # must match a name in NotionCache.income_subcategories
    account: str        # must match a name in NotionCache.accounts
    confidence: float   # 0.0–1.0

    _validate_amount = field_validator("amount")(_validate_amount)


class QueryIntent(BaseModel):
    type: str           # "query" | "log_text" | "log_income" | "unknown"
    text: str           # the original user message


class EmailTransaction(BaseModel):
    """Parsed result from a bank notification email."""
    type: str           # "expense" | "self_transfer" | "skip"
    description: str    # merchant name or recipient name
    amount: float       # main transaction amount in IDR
    admin_fee: float    # admin fee for self-transfers (0 if none)
    date: str           # YYYY-MM-DD parsed from email
    subcategory: str    # suggested subcategory name (matched against cache)
    account: str        # source account name (matched against cache)
    recipient_name: str = ""   # for transfers: recipient's name
    recipient_bank: str = ""   # for transfers: recipient's bank name
    skip_reason: str = ""      # why skipped (if type == "skip")


# ── Notion relation cache ─────────────────────────────────────────────────────

@dataclass
class NotionCache:
    # name → page URL (for writing relations)
    subcategories: dict[str, str] = field(default_factory=dict)
    accounts: dict[str, str] = field(default_factory=dict)
    months: dict[str, str] = field(default_factory=dict)
    years: dict[str, str] = field(default_factory=dict)

    # Income-specific relation caches (separate DB from expense subcategories)
    income_subcategories: dict[str, str] = field(default_factory=dict)
    income_months: dict[str, str] = field(default_factory=dict)
    income_years: dict[str, str] = field(default_factory=dict)

    # Category → list of subcategory names (for two-level picker)
    category_subcategories: dict[str, list[str]] = field(default_factory=dict)

    # amount in IDR (rounded to int) → {name, page_url, subcategory, account}
    # Loaded from the "Recurring Payment" Notion database (Active entries only)
    # Keyed by int to avoid float equality issues (e.g. 49999.99999 != 50000.0)
    recurring_payments: dict[int, dict] = field(default_factory=dict)

    def closest_subcategory(self, name: str) -> tuple[str, str] | None:
        """Fuzzy match subcategory name → (matched_name, page_url)."""
        return _fuzzy_match(name, self.subcategories)

    def closest_income_subcategory(self, name: str) -> tuple[str, str] | None:
        return _fuzzy_match(name, self.income_subcategories)

    def closest_account(self, name: str) -> tuple[str, str] | None:
        return _fuzzy_match(name, self.accounts)

    def month_url(self, month_name: str) -> tuple[str, str] | None:
        return _fuzzy_match(month_name, self.months)

    def year_url(self, year: str) -> tuple[str, str] | None:
        return _fuzzy_match(year, self.years)


def _fuzzy_match(name: str, options: dict[str, str]) -> tuple[str, str] | None:
    if not name or not options:
        return None
    name_lower = name.lower()
    # exact match first
    for k, v in options.items():
        if k.lower() == name_lower:
            return k, v
    # prefix match (minimum 2 chars to avoid single-letter accidental matches)
    if len(name_lower) >= 2:
        for k, v in options.items():
            if k.lower().startswith(name_lower) or name_lower.startswith(k.lower()):
                return k, v
    # partial match with minimum length guard to avoid single-char matches
    if len(name_lower) < 3:
        return None
    candidates = [
        (k, v) for k, v in options.items()
        if name_lower in k.lower() or k.lower() in name_lower
    ]
    if candidates:
        return max(candidates, key=lambda kv: len(kv[0]))
    return None


# ── User record (multi-tenant) ───────────────────────────────────────────────

@dataclass
class UserRecord:
    telegram_id: int
    owner_name: str
    notion_token: str
    expenses_ds: str | None = None
    subcategories_ds: str | None = None
    accounts_ds: str | None = None
    months_ds: str | None = None
    years_ds: str | None = None
    recurring_ds: str | None = None
    assets_ds: str | None = None
    income_ds: str | None = None
    income_subcategories_ds: str | None = None
    income_months_ds: str | None = None
    income_years_ds: str | None = None
    budget_ds: str | None = None
    categories_ds: str | None = None
    setup_step: str = "start"

    def db_ids(self) -> dict[str, str]:
        """Return non-None database IDs as a dict."""
        mapping = {
            "expenses_ds": self.expenses_ds,
            "subcategories_ds": self.subcategories_ds,
            "accounts_ds": self.accounts_ds,
            "months_ds": self.months_ds,
            "years_ds": self.years_ds,
            "recurring_ds": self.recurring_ds,
            "assets_ds": self.assets_ds,
            "income_ds": self.income_ds,
            "income_subcategories_ds": self.income_subcategories_ds,
            "income_months_ds": self.income_months_ds,
            "income_years_ds": self.income_years_ds,
            "budget_ds": self.budget_ds,
            "categories_ds": self.categories_ds,
        }
        return {k: v for k, v in mapping.items() if v is not None}

    @property
    def is_setup_complete(self) -> bool:
        return all(v is not None for v in [
            self.expenses_ds, self.subcategories_ds, self.accounts_ds,
            self.months_ds, self.years_ds, self.recurring_ds,
            self.income_ds, self.income_subcategories_ds,
            self.income_months_ds, self.income_years_ds, self.budget_ds,
            self.categories_ds,
        ])
