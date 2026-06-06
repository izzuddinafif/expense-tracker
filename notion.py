import httpx
from config import Config
from models import NotionCache, ExpenseEntry


NOTION_VERSION = "2022-06-28"
_HTTP_TIMEOUT = 30.0  # seconds


class NotionClient:
    def __init__(self, config: Config) -> None:
        self._headers = {
            "Authorization": f"Bearer {config.notion_token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }
        self._config = config

    async def _query_db(
        self, database_id: str, *, extra_payload: dict | None = None
    ) -> list[dict]:
        """Query all pages from a Notion database (handles pagination)."""
        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        results = []
        payload: dict = extra_payload.copy() if extra_payload else {}

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while True:
                resp = await client.post(url, headers=self._headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                results.extend(data["results"])
                if not data.get("has_more"):
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

        pages = await self._query_db(self._config.recurring_ds)
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

        cache.subcategories = await _load(self._config.subcategories_ds)
        cache.accounts = await _load(self._config.accounts_ds)
        cache.months = await _load(self._config.months_ds)
        cache.years = await _load(self._config.years_ds)
        cache.recurring_payments = await self._load_recurring(
            cache.subcategories, cache.accounts
        )

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
        date_parts = entry.date.split("-")
        year_str = date_parts[0]
        month_names = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ]
        month_str = month_names[int(date_parts[1]) - 1]

        subcategory_match = cache.closest_subcategory(entry.subcategory)
        account_match = cache.closest_account(entry.account)
        month_url = cache.month_url(month_str)
        year_url = cache.year_url(year_str)

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

        payload = {
            "parent": {"database_id": self._config.expenses_ds},
            "properties": properties,
        }

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                "https://api.notion.com/v1/pages",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["url"]

    async def fetch_expenses(self, owner: str) -> list[dict]:
        """Fetch expenses for a given owner, filtered on the Notion side."""
        payload = {
            "filter": {
                "property": "Description",
                "title": {"contains": f"[{owner}]"},
            }
        }
        pages = await self._query_db(self._config.expenses_ds, extra_payload=payload)
        result = []
        for p in pages:
            title_prop = p["properties"].get("Description", {})
            title = "".join(t["plain_text"] for t in title_prop.get("title", []))
            amount = p["properties"].get("Amount", {}).get("number", 0)
            date_prop = p["properties"].get("Date of Expense", {}).get("date") or {}
            result.append({
                "description": title.replace(f"[{owner}] ", ""),
                "amount": amount,
                "date": date_prop.get("start", ""),
            })
        return result

    async def fetch_assets(self) -> list[dict]:
        """Fetch all entries from the Assets database."""
        pages = await self._query_db(self._config.assets_ds)
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
