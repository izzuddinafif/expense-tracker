import asyncio
import logging
import re
import httpx
from typing import Any
from models import NotionCache, ExpenseEntry, IncomeEntry, UserRecord

log = logging.getLogger(__name__)

# Locks to prevent TOCTOU race on auto-creating month/year pages
_month_locks: dict[str, asyncio.Lock] = {}
_year_locks: dict[str, asyncio.Lock] = {}


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
            if not data.get("has_more") or pages >= 200:
                if pages >= 200:
                    log.warning("_query_db hit 200-page limit for %s (%d results so far) — truncated", database_id, len(results))
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
    ) -> dict[int, list[dict]]:
        """
        Load Active entries from the Recurring Payment database.
        Returns dict keyed by amount (IDR int) → {name, page_url, subcategory, account}.
        """
        # Build reverse maps: Notion page ID → human-readable name
        sub_id_to_name = {_url_to_id(url): name for name, url in subcategories.items()}
        acc_id_to_name = {_url_to_id(url): name for name, url in accounts.items()}

        pages = await self._query_db(self._db_ids["recurring_ds"])
        result: dict[int, list[dict]] = {}

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

            result.setdefault(int(round(amount)), []).append({
                "name": name,
                "page_url": p["url"],
                "subcategory": sub_id_to_name.get(sub_id or "", ""),
                "account": acc_id_to_name.get(acc_id or "", ""),
            })

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

    async def _ensure_month(self, month_name: str, cache: NotionCache) -> str | None:
        db_id = self._db_ids.get("months_ds")
        if not db_id:
            return None
        match = cache.month_url(month_name)
        if match:
            log.debug("_ensure_month: cache hit for '%s' → %s", month_name, match)
            return match[1]
        log.info(
            "_ensure_month: cache miss for '%s', available months: %s",
            month_name, list(cache.months.keys())[:5],
        )
        # Lock to prevent TOCTOU race — check again after acquiring
        lock = _month_locks.setdefault(month_name, asyncio.Lock())
        async with lock:
            match = cache.month_url(month_name)
            if match:
                return match[1]
            payload = {
                "parent": {"database_id": db_id},
                "properties": {
                    "title": {"title": [{"text": {"content": month_name}}]}
                },
            }
            try:
                data = await self._notion_post(
                    "https://api.notion.com/v1/pages", json=payload,
                )
                url = data["url"]
                cache.months[month_name] = url
                log.info("Notion WRITE auto-create month: %s → %s", month_name, url)
                return url
            except Exception as e:
                log.warning("Failed to auto-create month page %s: %s", month_name, e)
                return None

    async def _ensure_year(self, year_str: str, cache: NotionCache) -> str | None:
        db_id = self._db_ids.get("years_ds")
        if not db_id:
            return None
        match = cache.year_url(year_str)
        if match:
            return match[1]
        lock = _year_locks.setdefault(year_str, asyncio.Lock())
        async with lock:
            match = cache.year_url(year_str)
            if match:
                return match[1]
            payload = {
                "parent": {"database_id": db_id},
                "properties": {
                    "title": {"title": [{"text": {"content": year_str}}]}
                },
            }
            try:
                data = await self._notion_post(
                    "https://api.notion.com/v1/pages", json=payload,
                )
                url = data["url"]
                cache.years[year_str] = url
                log.info("Notion WRITE auto-create year: %s → %s", year_str, url)
                return url
            except Exception as e:
                log.warning("Failed to auto-create year page %s: %s", year_str, e)
                return None

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
        entry.date = _coerce_date(entry.date)
        
        year_str, month_str = _parse_date(entry.date)

        subcategory_match = cache.closest_subcategory(entry.subcategory)
        account_match = cache.closest_account(entry.account)
        month_url = await self._ensure_month(month_str, cache)
        year_url = await self._ensure_year(year_str, cache)

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
        else:
            log.warning("Expense '%s' — subcategory '%s' did not match any cache entry", entry.description, entry.subcategory)

        if account_match:
            _, acc_url = account_match
            properties["Accounts"] = {
                "relation": [{"id": _url_to_id(acc_url)}]
            }
        else:
            log.warning("Expense '%s' — account '%s' did not match any cache entry", entry.description, entry.account)

        if month_url:
            properties["Month"] = {
                "relation": [{"id": _url_to_id(month_url)}]
            }

        if year_url:
            properties["Year"] = {
                "relation": [{"id": _url_to_id(year_url)}]
            }

        if recurring_page_url:
            properties["Linked Recurring Payment"] = {
                "relation": [{"id": _url_to_id(recurring_page_url)}]
            }

        # Merchant field
        merchant_name = entry.merchant or _extract_merchant_from_description(entry.description)
        if merchant_name:
            properties["Merchant"] = {
                "rich_text": [{"text": {"content": merchant_name}}]
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
        entry.date = _coerce_date(entry.date)

        year_str, month_str = _parse_date(entry.date)

        subcategory_match = cache.closest_income_subcategory(entry.subcategory)
        account_match = cache.closest_account(entry.account)
        month_url = await self._ensure_month(month_str, cache)
        year_url = await self._ensure_year(year_str, cache)

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

        if month_url:
            properties["Month"] = {
                "relation": [{"id": _url_to_id(month_url)}]
            }

        if year_url:
            properties["Year"] = {
                "relation": [{"id": _url_to_id(year_url)}]
            }

        # WORKAROUND: Notion database automation overwrites Month during page creation.
        # Create without Month/Year relations, then PATCH them in separately.
        month_relation = properties.pop("Month", None)
        year_relation = properties.pop("Year", None)

        payload = {
            "parent": {"database_id": self._db_ids["income_ds"]},
            "properties": properties,
        }
        data = await self._notion_post(
            "https://api.notion.com/v1/pages", json=payload,
        )
        page_url = data["url"]
        log.info("Notion WRITE income: [%s] %s Rp %.0f → %s", owner, entry.description, entry.amount, page_url)

        # PATCH Month and Year relations after creation to avoid automation overwrite
        patch_props = {}
        if month_relation:
            patch_props["Month"] = month_relation
        if year_relation:
            patch_props["Year"] = year_relation
        if patch_props:
            patch_url = f"https://api.notion.com/v1/pages/{data['id']}"
            await self._notion_patch(patch_url, {"properties": patch_props})

        return page_url

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

    async def update_expense_merchant(self, page_id: str, merchant: str) -> None:
        await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {
                "properties": {
                    "Merchant": {
                        "rich_text": [{"text": {"content": merchant}}]
                    }
                }
            },
        )
        log.info("Notion WRITE update merchant: %s → %s", page_id, merchant)

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

    async def update_expense_account(
        self, page_id: str, account_name: str, cache: NotionCache
    ) -> str:
        match = cache.closest_account(account_name)
        if not match:
            raise ValueError(f"Account not found: {account_name}")
        _, acc_url = match
        data = await self._notion_patch(
            f"https://api.notion.com/v1/pages/{page_id}",
            {
                "properties": {
                    "Accounts": {
                        "relation": [{"id": _url_to_id(acc_url)}]
                    }
                }
            },
        )
        url = data.get("url", page_id)
        log.info("Notion WRITE update account: %s → %s (%s)", page_id, account_name, url)
        return url

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
        self, owner: str, amount: float, date: str, db_key: str = "expenses_ds"
    ) -> list[str]:
        """Find existing entries with similar amount (±1 IDR) + date (±1 day) + owner.
        Uses a range to avoid float precision issues.
        db_key: "expenses_ds" (default) or "income_ds" for income entries."""
        amount_prop, date_prop = {
            "expenses_ds": ("Amount", "Date of Expense"),
            "income_ds": ("Amount", "Date of Income"),
        }.get(db_key, ("Amount", "Date of Expense"))
        payload = {
            "filter": {
                "and": [
                    {"property": "Description", "title": {"contains": f"[{owner}]"}},
                    {"property": amount_prop, "number": {"greater_than_or_equal_to": amount - 1}},
                    {"property": amount_prop, "number": {"less_than_or_equal_to": amount + 1}},
                    {"property": date_prop, "date": {"equals": date}},
                ]
            }
        }
        pages = await self._query_db(self._db_ids[db_key], extra_payload=payload)
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

    async def fetch_recent_expenses(self, owner: str, cache: NotionCache | None = None, limit: int = 10) -> list[dict]:
        payload = {
            "filter": {
                "property": "Description",
                "title": {"contains": f"[{owner}]"},
            },
            "sorts": [{"property": "Date of Expense", "direction": "descending"}],
            "page_size": limit,
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
            })
        return result

    async def search_expenses(self, owner: str, keyword: str, cache: NotionCache | None = None) -> list[dict]:
        payload = {
            "filter": {
                "and": [
                    {"property": "Description", "title": {"contains": f"[{owner}]"}},
                    {"property": "Description", "title": {"contains": keyword}},
                ]
            }
        }
        pages = await self._query_db(self._db_ids["expenses_ds"], extra_payload=payload)
        sub_id_to_name = {_url_to_id(url): name for name, url in (cache.subcategories.items() if cache else {})}
        acc_id_to_name = {_url_to_id(url): name for name, url in (cache.accounts.items() if cache else {})}
        result = []
        for p in pages:
            title_prop = p["properties"].get("Description", {})
            title = "".join(t["plain_text"] for t in title_prop.get("title", []))
            amount = p["properties"].get("Amount", {}).get("number", 0)
            date_prop = p["properties"].get("Date of Expense", {}).get("date") or {}
            sub_rel = p["properties"].get("Expenses Sub-categories", {}).get("relation", [])
            sub_name = ""
            if sub_rel and sub_id_to_name:
                sub_name = sub_id_to_name.get(sub_rel[0]["id"], "")
            acc_rel = p["properties"].get("Accounts", {}).get("relation", [])
            acc_name = ""
            if acc_rel and acc_id_to_name:
                acc_name = acc_id_to_name.get(acc_rel[0]["id"], "")
            result.append({
                "description": title.replace(f"[{owner}] ", ""),
                "amount": amount,
                "date": date_prop.get("start", ""),
                "subcategory": sub_name,
                "account": acc_name,
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

    async def find_similar_by_merchant(
        self, owner: str, merchant: str, amount: float, date: str, cache: NotionCache
    ) -> list[dict]:
        """
        Find past expenses with the same merchant and similar amount (±20%).
        Used for merchant-based purchase prediction / duplicate detection.
        Returns list of matching expense pages with their properties.
        """
        if not merchant:
            return []

        # Query expenses filtered by owner and date range (last 90 days)
        from datetime import date as date_type, timedelta
        try:
            d = date_type.fromisoformat(date)
        except (ValueError, TypeError):
            return []
        since = (d - timedelta(days=90)).isoformat()

        filter_payload = {
            "and": [
                {
                    "property": "Description",
                    "title": {"contains": f"[{owner}]"}
                },
                {
                    "property": "Date of Expense",
                    "date": {"on_or_after": since}
                }
            ]
        }

        try:
            pages = await self._query_db(
                self._db_ids["expenses_ds"],
                extra_payload={"filter": filter_payload}
            )
        except Exception as e:
            log.warning(f"find_similar_by_merchant query failed: {e}")
            return []

        matches = []
        merchant_lower = merchant.lower().strip()
        for p in pages:
            props = p.get("properties", {})

            # Check merchant field
            merchant_text = ""
            for rt in props.get("Merchant", {}).get("rich_text", []):
                merchant_text += rt.get("plain_text", "")

            # Fallback: extract from description
            if not merchant_text:
                desc_text = ""
                for rt in props.get("Description", {}).get("title", []):
                    desc_text += rt.get("plain_text", "")
                merchant_text = _extract_merchant_from_description(desc_text)

            if not merchant_text:
                continue

            # Fuzzy merchant match
            if merchant_lower not in merchant_text.lower() and merchant_text.lower() not in merchant_lower:
                continue

            # Amount match within ±20%
            page_amount = props.get("Amount", {}).get("number")
            if page_amount is None:
                continue
            if abs(page_amount - amount) / max(amount, 1) > 0.2:
                continue

            # Get subcategory
            subcat_names = []
            for rel in props.get("Expenses Sub-categories", {}).get("relation", []):
                sub_id = rel.get("id", "")
                for name, url in cache.subcategories.items():
                    if _url_to_id(url) == sub_id:
                        subcat_names.append(name)
                        break

            matches.append({
                "id": p["id"],
                "url": p.get("url", ""),
                "description": self._extract_title(p),
                "amount": page_amount,
                "date": props.get("Date of Expense", {}).get("date", {}).get("start", ""),
                "merchant": merchant_text,
                "subcategories": subcat_names,
            })

        return matches


_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _coerce_date(value: Any) -> str:
    """Normalize a date value to YYYY-MM-DD string."""
    if hasattr(value, "isoformat"):
        date_str = value.isoformat()
        if isinstance(date_str, str):
            return date_str[:10]
        return str(date_str)[:10]
    if not isinstance(value, str):
        raise TypeError(f"Expected str for date, got {type(value).__name__}")
    return value


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
    # If the ID already has dashes (format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx), return as-is
    if "-" in part and len(part) == 36:
        return part
    # Strip any title slug prefix (slug-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX)
    if len(part) > 32:
        part = part[-32:]
    # Insert dashes if the ID has none
    if "-" not in part and len(part) == 32:
        return f"{part[:8]}-{part[8:12]}-{part[12:16]}-{part[16:20]}-{part[20:]}"
    return part


def _extract_merchant_from_description(description: str) -> str:
    """Extract merchant name from a description like '[Afif] SAKINAH SUPERMARKET' or '[Afif] Warung Emak Keputih'."""
    # Remove owner prefix [Name]
    desc = re.sub(r"^\[[^\]]+\]\s*", "", description).strip()
    # If it contains ' — ' or ' - ', the part before the first dash is likely the merchant
    for sep in [" — ", " - "]:
        if sep in desc:
            return desc.split(sep)[0].strip()
    return desc
