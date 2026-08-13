"""Durable last-known-good snapshots of Notion reference data."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from models import NotionCache

log = logging.getLogger(__name__)

_STRING_MAP_FIELDS = (
    "subcategories",
    "accounts",
    "months",
    "years",
    "income_subcategories",
    "income_months",
    "income_years",
)


@dataclass(frozen=True)
class ReferenceLoadResult:
    cache: NotionCache
    source: str
    error: Exception | None = None


async def load_resilient_cache(
    store: "ReferenceStore",
    user_id: int,
    remote_loader: Callable[[], Awaitable[NotionCache]],
    *,
    timeout: float,
    prefer_snapshot: bool = False,
) -> ReferenceLoadResult:
    """Refresh remotely, falling back to the last valid SQLite snapshot."""
    snapshot = await store.load(user_id)
    if prefer_snapshot and snapshot is not None:
        return ReferenceLoadResult(snapshot, "snapshot")
    try:
        cache = await asyncio.wait_for(remote_loader(), timeout=timeout)
    except Exception as exc:
        return ReferenceLoadResult(
            snapshot or NotionCache(),
            "snapshot" if snapshot is not None else "empty",
            exc,
        )
    try:
        await store.save(user_id, cache)
    except Exception:
        # A successful remote cache remains useful for this process even when a
        # transient SQLite write fails.
        log.exception("Failed to persist Notion cache snapshot for user %s", user_id)
    return ReferenceLoadResult(cache, "remote")


class ReferenceStore:
    def __init__(self, database: Any) -> None:
        self._conn = database._conn

    async def save(self, user_id: int, cache: NotionCache) -> None:
        payload = {
            field: dict(getattr(cache, field))
            for field in _STRING_MAP_FIELDS
        }
        payload["category_subcategories"] = {
            name: list(values)
            for name, values in cache.category_subcategories.items()
        }
        payload["recurring_payments"] = {
            str(amount): [dict(item) for item in values]
            for amount, values in cache.recurring_payments.items()
        }
        await self._conn.execute(
            "INSERT INTO notion_cache_snapshots(user_id,cache_json,refreshed_at) "
            "VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
            "cache_json=excluded.cache_json,refreshed_at=excluded.refreshed_at",
            (
                user_id,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self._conn.commit()

    async def load(self, user_id: int) -> NotionCache | None:
        row = await (
            await self._conn.execute(
                "SELECT cache_json FROM notion_cache_snapshots WHERE user_id=?",
                (user_id,),
            )
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["cache_json"])
            if not isinstance(payload, dict):
                raise ValueError("snapshot root must be an object")
            string_maps = {
                field: self._string_map(payload.get(field, {}), field)
                for field in _STRING_MAP_FIELDS
            }
            categories_raw = payload.get("category_subcategories", {})
            if not isinstance(categories_raw, dict):
                raise ValueError("category_subcategories must be an object")
            categories: dict[str, list[str]] = {}
            for name, values in categories_raw.items():
                if not isinstance(name, str) or not isinstance(values, list) or not all(
                    isinstance(value, str) for value in values
                ):
                    raise ValueError("invalid category_subcategories entry")
                categories[name] = list(values)

            recurring_raw = payload.get("recurring_payments", {})
            if not isinstance(recurring_raw, dict):
                raise ValueError("recurring_payments must be an object")
            recurring: dict[int, list[dict]] = {}
            for amount, values in recurring_raw.items():
                amount_idr = int(amount)
                if amount_idr <= 0 or not isinstance(values, list) or not all(
                    isinstance(value, dict) for value in values
                ):
                    raise ValueError("invalid recurring_payments entry")
                recurring[amount_idr] = [dict(value) for value in values]

            return NotionCache(
                **string_maps,
                category_subcategories=categories,
                recurring_payments=recurring,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            log.warning(
                "Ignoring malformed Notion cache snapshot for user %s: %s",
                user_id,
                exc,
            )
            return None

    @staticmethod
    def _string_map(value: object, field: str) -> dict[str, str]:
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError(f"{field} must map strings to strings")
        return dict(value)
