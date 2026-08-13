"""Read-only local ledger versus Notion transaction reconciliation.

The local transaction UUID is the canonical ``Transaction ID`` stored on the
corresponding Notion Expenses or Income page.  This module deliberately does
not repair differences: callers can inspect the returned report and decide how
to resolve them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from notion import TRANSACTION_ID_PROPERTY


@dataclass(frozen=True)
class LocalTransaction:
    """The local fields needed to reconcile one durable ledger record."""

    transaction_id: str
    kind: str
    status: str
    notion_page_id: str | None


@dataclass(frozen=True)
class RemoteTransactionPage:
    """An active Notion transaction page and its declared transaction ID."""

    page_id: str
    kind: str
    transaction_id: str | None


@dataclass
class ReconciliationReport:
    """Differences found without changing either the ledger or Notion."""

    missing_remote: list[LocalTransaction] = field(default_factory=list)
    unexpected_remote: list[RemoteTransactionPage] = field(default_factory=list)
    duplicate_ids: dict[str, list[RemoteTransactionPage]] = field(default_factory=dict)
    kind_mismatches: list[LocalTransaction] = field(default_factory=list)
    notion_page_id_mismatches: list[LocalTransaction] = field(default_factory=list)
    voided_pages_still_active: list[LocalTransaction] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """Whether the two systems agree for all checked transaction records."""
        return not any(
            (
                self.missing_remote,
                self.unexpected_remote,
                self.duplicate_ids,
                self.kind_mismatches,
                self.notion_page_id_mismatches,
                self.voided_pages_still_active,
            )
        )


class _Database(Protocol):
    _conn: Any


class _NotionClient(Protocol):
    _db_ids: dict[str, str]

    async def _query_db(
        self, database_id: str, *, extra_payload: dict | None = None
    ) -> list[dict]: ...


def _normalise_id(value: str | None) -> str:
    """Make UUID-style Notion page IDs comparable with or without dashes."""
    return (value or "").replace("-", "").strip().lower()


def _transaction_id(page: dict[str, Any]) -> str | None:
    """Extract the rich-text Transaction ID property from a Notion page."""
    property_value = page.get("properties", {}).get(TRANSACTION_ID_PROPERTY, {})
    fragments = property_value.get("rich_text", [])
    text = "".join(
        fragment.get("plain_text")
        or fragment.get("text", {}).get("content", "")
        for fragment in fragments
    ).strip()
    return text or None


def _remote_pages(kind: str, pages: list[dict[str, Any]]) -> list[RemoteTransactionPage]:
    """Convert active query results into the small, comparison-safe shape."""
    result: list[RemoteTransactionPage] = []
    for page in pages:
        # Normal database queries omit archived pages, but retaining this check
        # keeps the reconciliation correct for alternate clients and test fakes.
        if page.get("archived") or page.get("in_trash"):
            continue
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            continue
        result.append(
            RemoteTransactionPage(
                page_id=page_id,
                kind=kind,
                transaction_id=_transaction_id(page),
            )
        )
    return result


async def reconcile_transactions(
    database: _Database, notion: _NotionClient, user_id: int
) -> ReconciliationReport:
    """Compare one user's confirmed and voided transactions with Notion.

    Only ``expense`` and ``income`` rows participate because those are the two
    Notion transaction databases.  The database operation is a ``SELECT`` and
    the Notion operations are database queries; this function never writes,
    archives, retries, or queues sync work.
    """
    cursor = await database._conn.execute(
        "SELECT id, kind, status, notion_page_id FROM transactions "
        "WHERE user_id=? AND kind IN ('expense', 'income') "
        "AND status IN ('confirmed', 'voided')",
        (user_id,),
    )
    local_rows = [
        LocalTransaction(
            transaction_id=row["id"],
            kind=row["kind"],
            status=row["status"],
            notion_page_id=row["notion_page_id"],
        )
        for row in await cursor.fetchall()
    ]

    expense_pages, income_pages = await asyncio.gather(
        notion._query_db(notion._db_ids["expenses_ds"]),
        notion._query_db(notion._db_ids["income_ds"]),
    )
    remote_rows = _remote_pages("expense", expense_pages)
    remote_rows.extend(_remote_pages("income", income_pages))

    remote_by_id: dict[str, list[RemoteTransactionPage]] = {}
    report = ReconciliationReport()
    for remote in remote_rows:
        normalized = _normalise_id(remote.transaction_id)
        if not normalized:
            # A page with no Transaction ID cannot be safely matched to the
            # ledger and is therefore an unexpected remote transaction page.
            report.unexpected_remote.append(remote)
            continue
        remote_by_id.setdefault(normalized, []).append(remote)

    local_by_id = {
        _normalise_id(local.transaction_id): local for local in local_rows
    }
    for transaction_id, pages in remote_by_id.items():
        if len(pages) > 1:
            # Preserve the ID as it appears in Notion for an actionable
            # report, while using its normalized form for matching.
            report.duplicate_ids[pages[0].transaction_id or transaction_id] = pages
        if transaction_id not in local_by_id:
            report.unexpected_remote.extend(pages)

    for normalized_id, local in local_by_id.items():
        remote_matches = remote_by_id.get(normalized_id, [])
        if local.status == "confirmed" and not remote_matches:
            report.missing_remote.append(local)
        if local.status == "voided" and remote_matches:
            report.voided_pages_still_active.append(local)
        if remote_matches and any(page.kind != local.kind for page in remote_matches):
            report.kind_mismatches.append(local)
        if local.notion_page_id and remote_matches:
            remote_page_ids = {_normalise_id(page.page_id) for page in remote_matches}
            if _normalise_id(local.notion_page_id) not in remote_page_ids:
                report.notion_page_id_mismatches.append(local)

    return report


async def reconcile_user_transactions(
    database: _Database, notion: _NotionClient, user_id: int
) -> ReconciliationReport:
    """Compatibility-oriented explicit name for :func:`reconcile_transactions`."""
    return await reconcile_transactions(database, notion, user_id)
