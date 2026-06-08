import asyncio
import logging
import httpx
from models import NotionCache, ExpenseEntry, IncomeEntry, UserRecord

log = logging.getLogger(__name__)


NOTION_VERSION = "2022-06-28"
_HTTP_TIMEOUT = 30.0  # seconds

# Map from db_ids key → expected Notion database title (from the template)
DB_NAME_MAP = {
    "expenses_ds":              "Expenses",
    "subcategories_ds":         "Sub-categories",
    "accounts_ds":              "Accounts",
    "months_ds":                "Month",
    "years_ds":                 "Year",
    "recurring_ds":             "Recurring Payment",
    "assets_ds":                "Assets",
    "income_ds":                "Income",
    "budget_ds":                "Budget",
    "categories_ds":            "Categories",
}


class NotionClient:
    def __init__(self, notion_token: str, db_ids: dict[str, str]) -> None:
        self._headers = {
            "Authorization": f"Bearer {notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._db_ids = db_ids
        self._http = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)

    @classmethod
    def from_user(cls, user: UserRecord) -> "NotionClient":
        """Build a NotionClient from a UserRecord (multi-tenant)."""
        return cls(notion_token=user.notion_token, db_ids=user.db_ids())

    async def discover_databases(self) -> dict[str, str]:
        """
        Search the user's Notion workspace for all databases by name.
        Returns {field_name: database_id} for every DB found.
        Raises RuntimeError listing any DBs that couldn't be found.
        """
        url = "https://api.notion.com/v1/search"
        payload = {
            "filter": {"value": "database", "property": "object"},
            "page_size": 100,
        }

        all_databases: dict[str, str] = {}  # title → database_id
        start_cursor = None

        while True:
            body = payload.copy()
            if start_cursor:
                body["start_cursor"] = start_cursor
            data = await self._notion_post(url, json=body)

            for db in data.get("results", []):
                title_parts = db.get("title", [])
                title = "".join(t.get("plain_text", "") for t in title_parts).strip()
                if title:
                    all_databases[title] = db["id"]

            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")

        # Match discovered databases to expected names
        found: dict[str, str] = {}
        missing: list[str] = []
        OPTIONAL_DBS = {"assets_ds"}  # nice-to-have, won't block setup

        for field_name, expected_title in DB_NAME_MAP.items():
            db_id = all_databases.get(expected_title)
            if db_id:
                found[field_name] = db_id
            elif field_name in OPTIONAL_DBS:
                log.info(f"Optional database '{expected_title}' not found — skipping")
            else:
                missing.append(expected_title)

        # Handle shared databases: income_subcategories uses same DB as subcategories
        # income_months uses same DB as months, income_years uses same DB as years
        if "subcategories_ds" in found and "income_subcategories_ds" not in found:
            found["income_subcategories_ds"] = found["subcategories_ds"]
        if "months_ds" in found and "income_months_ds" not in found:
            found["income_months_ds"] = found["months_ds"]
        if "years_ds" in found and "income_years_ds" not in found:
            found["income_years_ds"] = found["years_ds"]

        if missing:
            raise RuntimeError(
                f"Could not find these Notion databases: {', '.join(missing)}. "
                "Please share the template page with your Notion integration."
            )

        return found

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _notion_post(self, url: str, json: dict) -> dict:
        """POST ke Notion dengan retry untuk transport error, 429, dan 5xx."""
        last_resp = None
        last_exc = None
        delay = 1.0
        for attempt in range(3):
            try:
                resp = await self._http.post(url, headers=self._headers, json=json)
            except httpx.TransportError as e:
                last_exc = e
                log.warning(f"Notion transport error (attempt {attempt + 1}): {e}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            last_resp = resp
            retry_after = float(resp.headers.get("Retry-After", delay))
            await asyncio.sleep(retry_after)
            delay *= 2
        if last_exc:
            raise last_exc
        last_resp.raise_for_status()
        return last_resp.json()

    async def _query_db(
        self, database_id: str, *, extra_payload: dict | None = None
    ) -> list[dict]:
        """Query all pages from a Notion database (handles pagination)."""
        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        results = []
        payload: dict = extra_payload.copy() if extra_payload else {}
        pages = 0

        while True:
            data = await self._notion_post(url, json=payload)
            results.extend(data["results"])
            pages += 1
            if not data.get("has_more") or pages >= 100:
                break
            payload["start_cursor"] = data["next_cursor"]

        return results

    def _extract_title(self, page: dict) -> str:
        """Extract plain text title from a Notion page."""
        for prop in page["properties"].values():
            if prop["type"] == "title":
                return "".join(p["plain_text"] for p in prop["title"])
        return ""

    def _extract_relation_id(self, page: dict, prop_name: str) -> str | None:
        """Extract the first related page ID from a relation property."""
        prop = page["properties"].get(prop_name, {})
        if prop.get("type") == "relation":
            relations = prop.get("relation", [])
            if relations:
                return relations[0]["id"]
        return None

    async def _load_recurring(
        self,
        subcategories: dict[str, str],
        accounts: dict[str, str],
    ) -> dict[int, dict]:
        """
        Load Active entries from the Recurring Payment database.
        Returns dict keyed by amount (IDR int) → {name, page_url, subcategory, account}.
        """
        # Build reverse maps: Notion page ID → human-readable name
        sub_id_to_name = {_url_to_id(url): name for name, url in subcategories.items()}
        acc_id_to_name = {_url_to_id(url): name for name, url in accounts.items()}

        pages = await self._query_db(self._db_ids["recurring_ds"])
        result: dict[int, dict] = {}

        for p in pages:
            # Only Active entries
            status_prop = p["properties"].get("Status", {})
            status_name = status_prop.get("status", {}).get("name", "")
            if status_name != "Active":
                continue

            amount = p["properties"].get("Amount", {}).get("number")
            name = self._extract_title(p)
            if amount is None or not name:
                continue

            sub_id = self._extract_relation_id(p, "🥡 Sub-categories")
            acc_id = self._extract_relation_id(p, "🧾 Accounts")

            result[int(round(amount))] = {
                "name": name,
                "page_url": p["url"],
                "subcategory": sub_id_to_name.get(sub_id or "", ""),
                "account": acc_id_to_name.get(acc_id or "", ""),
            }

        return result

    async def load_cache(self) -> NotionCache:
        """Fetch all relation options and return a populated cache."""
        cache = NotionCache()

        async def _load(ds_id: str) -> dict[str, str]:
            pages = await self._query_db(ds_id)
            return {self._extract_title(p): p["url"] for p in pages}

        # subcategories and accounts must be ready before _load_recurring
        cache.subcategories, cache.accounts = await asyncio.gather(
            _load(self._db_ids["subcategories_ds"]),
            _load(self._db_ids["accounts_ds"]),
        )
        (
            cache.months,
            cache.years,
            cache.income_subcategories,
            cache.income_months,
            cache.income_years,
            cache.recurring_payments,
        ) = await asyncio.gather(
            _load(self._db_ids["months_ds"]),
            _load(self._db_ids["years_ds"]),
            _load(self._db_ids["income_subcategories_ds"]),
            _load(self._db_ids["income_months_ds"]),
            _load(self._db_ids["income_years_ds"]),
            self._load_recurring(cache.subcategories, cache.accounts),
        )

        # Build category → subcategory mapping
        subcat_id_to_name = {
            _url_to_id(url): name for name, url in cache.subcategories.items()
        }
        cat_pages = await self._query_db(self._db_ids["categories_ds"])
        for p in cat_pages:
            cat_name = self._extract_title(p)
            subcat_ids = [
                r["id"]
                for r in p["properties"].get("🥡 Sub-categories", {}).get("relation", [])
            ]
            subcats = [
                subcat_id_to_name[sid]
                for sid in subcat_ids
                if sid in subcat_id_to_name
            ]
            if subcats:
                cache.category_subcategories[cat_name] = subcats

        return cache

    async def log_expense(
        self,
        entry: ExpenseEntry,
        owner: str,
        cache: NotionCache,
        recurring_page_url: str | None = None,
    ) -> str:
        """
        Create a new expense entry in Notion. Returns the page URL.

        If recurring_page_url is provided, the expense is linked to that
        Recurring Payment entry via the 'Linked Recurring Payment' relation.
        """
        year_str, month_str = _parse_date(entry.date)

        subcategory_match = cache.closest_subcategory(entry.subcategory)
        account_match = cache.closest_account(entry.account)
        month_match = cache.month_url(month_str)
        year_match = cache.year_url(year_str)

        properties: dict = {
            "Description": {
                "title": [{"text": {"content": f"[{owner}] {entry.description}"}}]
            },
            "Amount": {"number": entry.amount},
            "Date of Expense": {"date": {"start": entry.date}},
        }

        if subcategory_match:
            _, sub_url = subcategory_match
            properties["Expenses Sub-categories"] = {
                "relation": [{"id": _url_to_id(sub_url)}]
            }

        if account_match:
            _, acc_url = account_match
            properties["Accounts"] = {
                "relation": [{"id": _url_to_id(acc_url)}]
            }

        if month_match:
            _, month_url = month_match
            properties["Month"] = {
                "relation": [{"id": _url_to_id(month_url)}]
            }

        if year_match:
            _, year_url = year_match
            properties["Year"] = {
                "relation": [{"id": _url_to_id(year_url)}]
            }

        if recurring_page_url:
            properties["Linked Recurring Payment"] = {
                "relation": [{"id": _url_to_id(recurring_page_url)}]
            }

        payload = {
            "parent": {"database_id": self._db_ids["expenses_ds"]},
            "properties": properties,
        }

        data = await self._notion_post(
            "https://api.notion.com/v1/pages", json=payload,
        )
        log.info(
            "Notion WRITE expense: [%s] %s Rp %.0f → %s",
            owner, entry.description, entry.amount, data["url"],
        )
        return data["url"]

    async def log_income(
        self,
        entry: IncomeEntry,
        owner: str,
        cache: NotionCache,
    ) -> str:
        """Create a new income entry in Notion. Returns the page URL."""
        year_str, month_str = _parse_date(entry.date)

        subcategory_match = cache.closest_income_subcategory(entry.subcategory)
        account_match = cache.closest_account(entry.account)
        month_match = cache.month_url(month_str)
        year_match = cache.year_url(year_str)

        properties: dict = {
            "Description": {
                "title": [{"text": {"content": f"[{owner}] {entry.description}"}}]
            },
            "Amount": {"number": entry.amount},
            "Date of Income": {"date": {"start": entry.date}},
        }

        if subcategory_match:
            _, sub_url = subcategory_match
            properties["Income Sub-categories"] = {
                "relation": [{"id": _url_to_id(sub_url)}]
            }

        if account_match:
            _, acc_url = account_match
            properties["Accounts"] = {
                "relation": [{"id": _url_to_id(acc_url)}]
            }

        if month_match:
            _, month_url = month_match
            properties["Month"] = {
                "relation": [{"id": _url_to_id(month_url)}]
            }

        if year_match:
            _, year_url = year_match
            properties["Year"] = {
                "relation": [{"id": _url_to_id(year_url)}]
            }

        payload = {
            "parent": {"database_id": self._db_ids["income_ds"]},
            "properties": properties,
        }

        data = await self._notion_post(
            "https://api.notion.com/v1/pages", json=payload,
        )
        log.info(
            "Notion WRITE income: [%s] %s Rp %.0f → %s",
            owner, entry.description, entry.amount, data["url"],
        )
        return data["url"]

    async def _notion_patch(self, url: str, json: dict) -> dict:
        """PATCH a Notion page with retry logic (same as _notion_post)."""
        last_resp = None
        last_exc = None
        delay = 1.0
        for attempt in range(3):
            try:
                resp = await self._http.patch(url, headers=self._headers, json=json)
            except httpx.TransportError as e:
                last_exc = e
                log.warning(f"Notion transport error (attempt {attempt + 1}): {e}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code < 500 and resp.status_code != 429:
                resp.raise_for_status()
                return resp.json()
            last_resp = resp
            retry_after = float(resp.headers.get("Retry-After", delay))
            await asyncio.sleep(retry_after)
            delay *= 2
        if last_exc:
            raise last_exc
        last_resp.raise_for_status()
        return last_resp.json()

    async def archive_page(self, page_id: str) -> None:
        """Archive (soft-delete) a Notion page by ID. Used for undo."""
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {"archived": True},
        )
        log.info("Notion WRITE archive page: %s", page_id)

    async def update_expense_title(self, page_id: str, description: str, owner: str) -> None:
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {
                "properties": {
                    "Description": {
                        "title": [{"text": {"content": f"[{owner}] {description}"}}]
                    }
                }
            },
        )
        log.info("Notion WRITE update title: %s → %s", page_id, description)

    async def update_expense_amount(self, page_id: str, amount: float) -> None:
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {"properties": {"Amount": {"number": amount}}},
        )
        log.info("Notion WRITE update amount: %s → %.0f", page_id, amount)

    async def update_expense_date(self, page_id: str, date: str) -> None:
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {"properties": {"Date of Expense": {"date": {"start": date}}}},
        )
        log.info("Notion WRITE update date: %s → %s", page_id, date)

    async def update_expense_subcategory(
        self, page_id: str, subcategory_name: str, cache: NotionCache
    ) -> None:
        match = cache.closest_subcategory(subcategory_name)
        if not match:
            raise ValueError(f"Subcategory not found: {subcategory_name}")
        _, subcat_url = match
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {
                "properties": {
                    "Expenses Sub-categories": {
                        "relation": [{"id": _url_to_id(subcat_url)}]
                    }
                }
            },
        )
        log.info("Notion WRITE update subcategory: %s → %s", page_id, subcategory_name)

    async def fetch_duplicates(
        self, owner: str, amount: float, date: str
    ) -> list[str]:
        """Find existing expenses with the same amount + date + owner."""
        payload = {
            "filter": {
                "and": [
                    {"property": "Description", "title": {"contains": f"[{owner}]"}},
                    {"property": "Amount", "number": {"equals": amount}},
                    {"property": "Date of Expense", "date": {"equals": date}},
                ]
            }
        }
        pages = await self._query_db(self._db_ids["expenses_ds"], extra_payload=payload)
        result = []
        for p in pages:
            title_prop = p["properties"].get("Description", {})
            title = "".join(t["plain_text"] for t in title_prop.get("title", []))
            result.append(title.replace(f"[{owner}] ", ""))
        return result

    async def fetch_expenses(self, owner: str, cache: NotionCache | None = None) -> list[dict]:
        """Fetch expenses for a given owner, filtered on the Notion side."""
        payload = {
            "filter": {
                "property": "Description",
                "title": {"contains": f"[{owner}]"},
            }
        }
        pages = await self._query_db(self._db_ids["expenses_ds"], extra_payload=payload)
        sub_id_to_name = {_url_to_id(url): name for name, url in (cache.subcategories.items() if cache else {})}
        result = []
        for p in pages:
            title_prop = p["properties"].get("Description", {})
            title = "".join(t["plain_text"] for t in title_prop.get("title", []))
            amount = p["properties"].get("Amount", {}).get("number", 0)
            date_prop = p["properties"].get("Date of Expense", {}).get("date") or {}
            sub_rel = p["properties"].get("Expenses Sub-categories", {}).get("relation", [])
            sub_name = ""
            if sub_rel:
                sid = sub_rel[0]["id"]
                sub_name = sub_id_to_name.get(sid, "")
            result.append({
                "description": title.replace(f"[{owner}] ", ""),
                "amount": amount,
                "date": date_prop.get("start", ""),
                "subcategory": sub_name,
                "url": p["url"],
            })
        return result

    async def search_expenses(self, owner: str, keyword: str) -> list[dict]:
        payload = {
            "filter": {
                "and": [
                    {"property": "Description", "title": {"contains": f"[{owner}]"}},
                    {"property": "Description", "title": {"contains": keyword}},
                ]
            }
        }
        pages = await self._query_db(self._db_ids["expenses_ds"], extra_payload=payload)
        result = []
        for p in pages:
            title_prop = p["properties"].get("Description", {})
            title = "".join(t["plain_text"] for t in title_prop.get("title", []))
            amount = p["properties"].get("Amount", {}).get("number", 0)
            date_prop = p["properties"].get("Date of Expense", {}).get("date") or {}
            sub_rel = p["properties"].get("Expenses Sub-categories", {}).get("relation", [])
            result.append({
                "description": title.replace(f"[{owner}] ", ""),
                "amount": amount,
                "date": date_prop.get("start", ""),
                "url": p["url"],
            })
        return result

    async def fetch_assets(self) -> list[dict]:
        """Fetch all entries from the Assets database."""
        assets_ds = self._db_ids.get("assets_ds")
        if not assets_ds:
            return []
        try:
            pages = await self._query_db(assets_ds)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                log.warning("Assets database not found (404) — treating as empty")
                return []
            raise
        result = []
        for p in pages:
            props = p["properties"]
            name = self._extract_title(p)
            result.append({
                "name": name,
                "type": props.get("Type", {}).get("select", {}).get("name", ""),
                "quantity": props.get("Quantity", {}).get("number"),
                "unit": "".join(t["plain_text"] for t in props.get("Unit", {}).get("rich_text", [])),
                "value_idr": props.get("Value IDR", {}).get("number"),
                "last_updated": (props.get("Last Updated", {}).get("date") or {}).get("start", ""),
                "notes": "".join(t["plain_text"] for t in props.get("Notes", {}).get("rich_text", [])),
            })
        return result

    async def fetch_budgets(self, cache: NotionCache | None = None) -> list[dict]:
        """Fetch all entries from the Budget database."""
        sub_id_to_name: dict[str, str] | None = None
        if cache is not None:
            sub_id_to_name = {_url_to_id(url): name for name, url in cache.subcategories.items()}

        pages = await self._query_db(self._db_ids["budget_ds"])
        result = []
        for p in pages:
            props = p["properties"]
            name = self._extract_title(p)
            budget_amount = props.get("Amount (Input here)", {}).get("number")
            period = props.get("Period", {}).get("select", {}) or {}
            spent = props.get("Amount Spent within Period", {}).get("formula", {}).get("number")
            pct = props.get("Percentage", {}).get("formula", {}).get("number")

            sub_names = []
            for rel in props.get("Sub-categories", {}).get("relation", []):
                sub_id = rel.get("id", "")
                sub_name = sub_id_to_name.get(sub_id, "") if sub_id_to_name else ""
                if sub_name:
                    sub_names.append(sub_name)

            if budget_amount is not None:
                result.append({
                    "name": name,
                    "budget": budget_amount,
                    "period": period.get("name", ""),
                    "spent": spent or 0,
                    "percentage": pct or 0,
                    "subcategories": sub_names,
                })
        return result


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _parse_date(date_str: str) -> tuple[str, str]:
    """Parse an ISO date string and return (year_str, month_name).

    Raises ValueError on malformed input so callers can surface it early.
    """
    parts = date_str.split("-")
    if len(parts) != 3:
        raise ValueError(f"Expected YYYY-MM-DD, got {date_str!r}")
    month_idx = int(parts[1]) - 1
    if not 0 <= month_idx <= 11:
        raise ValueError(f"Month out of range in date {date_str!r}")
    return parts[0], _MONTH_NAMES[month_idx]


def _url_to_id(url: str) -> str:
    """Extract Notion page ID (with dashes) from a URL or bare ID."""
    clean = url.rstrip("/")
    part = clean.split("/")[-1].split("?")[0]
    # strip any title slug prefix (slug-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX)
    if len(part) > 32:
        part = part[-32:]
    # insert dashes if the ID has none
    if "-" not in part and len(part) == 32:
        return f"{part[:8]}-{part[8:12]}-{part[12:16]}-{part[16:20]}-{part[20:]}"
    return part
