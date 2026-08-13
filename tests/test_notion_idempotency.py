"""Focused contracts for durable, idempotent Notion delivery.

These tests intentionally exercise the outbox boundary with small fakes.  They
are kept separate from the legacy sync tests so the retry/idempotency contract
can evolve without coupling it to HTTP fixtures.
"""

from datetime import datetime, timezone

import httpx
import pytest

import notion as notion_module
from models import ExpenseEntry, NotionCache, UserRecord
from notion import NotionClient
from notion_sync import NotionSyncWorker


TRANSACTION_ID = "tx-ambiguous-001"
PAGE_URL = "https://notion.so/page-001"


def _title_page(title: str, page_id: str) -> dict:
    return {
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "properties": {
            "Name": {
                "type": "title",
                "title": [{"plain_text": title}],
            }
        },
    }


def _client() -> NotionClient:
    client = NotionClient.__new__(NotionClient)
    client._db_ids = {"expenses_ds": "expenses-db"}

    async def no_relation(*_args):
        return None

    client._ensure_month = no_relation
    client._ensure_year = no_relation
    return client


def _entry() -> ExpenseEntry:
    return ExpenseEntry(
        description="subscription", amount=12000, date="2026-01-01",
        subcategory="Other", account="Cash", confidence=1, merchant="Service",
    )


@pytest.mark.asyncio
async def test_ambiguous_create_reconciles_transaction_id_before_another_post():
    """The real POST helper must not blindly retry a page create."""
    client = _client()
    client._headers = {}
    create_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, query_calls
        if request.url.path.endswith("/query"):
            query_calls += 1
            assert TRANSACTION_ID.encode() in request.content
            results = [] if query_calls == 1 else [{"url": PAGE_URL, "id": "page-001"}]
            return httpx.Response(200, json={"results": results, "has_more": False})
        assert request.url.path == "/v1/pages"
        create_calls += 1
        return httpx.Response(500, json={"message": "accepted but unavailable"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def repair_existing(page_url, *_args, **_kwargs):
        return page_url

    client._update_expense = repair_existing
    try:
        assert await client.upsert_expense(
            _entry(),
            "Afif",
            NotionCache(),
            TRANSACTION_ID,
        ) == PAGE_URL
    finally:
        await client.aclose()

    assert create_calls == 1
    assert query_calls == 2


@pytest.mark.asyncio
async def test_create_retries_only_after_a_transaction_id_reconciliation(monkeypatch):
    """A no-match reconciliation is required before a second create request."""
    client = _client()
    client._headers = {}
    requests = []

    async def no_sleep(_delay):
        return None

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        if requests.count("/v1/pages") == 1:
            return httpx.Response(503, json={"message": "unavailable"})
        return httpx.Response(201, json={"url": PAGE_URL, "id": "page-001"})

    monkeypatch.setattr(notion_module.asyncio, "sleep", no_sleep)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.upsert_expense(
            _entry(), "Afif", NotionCache(), TRANSACTION_ID
        ) == PAGE_URL
    finally:
        await client.aclose()

    assert requests == [
        "/v1/databases/expenses-db/query",
        "/v1/pages",
        "/v1/databases/expenses-db/query",
        "/v1/pages",
    ]


@pytest.mark.asyncio
async def test_multiple_transaction_id_matches_abort_before_page_creation():
    """A corrupt remote ID index must not select an arbitrary page to update."""
    client = _client()
    client._headers = {}
    create_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        if request.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": PAGE_URL, "id": "page-001"},
                        {"url": "https://notion.so/page-002", "id": "page-002"},
                    ],
                    "has_more": False,
                },
            )
        create_calls += 1
        return httpx.Response(201, json={"url": PAGE_URL})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="Multiple Notion pages"):
            await client.upsert_expense(
                _entry(), "Afif", NotionCache(), TRANSACTION_ID
            )
    finally:
        await client.aclose()

    assert create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "cache_field", "database_id", "title"),
    [
        ("_ensure_month", "months", "months-db", "July"),
        ("_ensure_year", "years", "years-db", "2026"),
    ],
)
async def test_ambiguous_relation_create_reconciles_exact_title(
    method, cache_field, database_id, title
):
    client = NotionClient.__new__(NotionClient)
    client._headers = {}
    client._db_ids = {"months_ds": "months-db", "years_ds": "years-db"}
    create_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, query_calls
        if request.url.path.endswith("/query"):
            query_calls += 1
            assert f'"equals":"{title}"'.encode() in request.content
            results = [] if query_calls == 1 else [_title_page(title, "relation-1")]
            return httpx.Response(200, json={"results": results, "has_more": False})
        create_calls += 1
        return httpx.Response(503, json={"message": "accepted but unavailable"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = NotionCache()
    try:
        result = await getattr(client, method)(title, cache)
    finally:
        await client.aclose()

    assert result == "https://notion.so/relation-1"
    assert getattr(cache, cache_field)[title] == result
    assert create_calls == 1
    assert query_calls == 2


@pytest.mark.asyncio
async def test_ambiguous_relation_title_reconciliation_aborts():
    client = NotionClient.__new__(NotionClient)
    client._headers = {}
    client._db_ids = {"months_ds": "months-db"}
    create_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, query_calls
        if request.url.path.endswith("/query"):
            query_calls += 1
            results = [] if query_calls == 1 else [
                _title_page("July", "relation-1"),
                _title_page("July", "relation-2"),
            ]
            return httpx.Response(
                200,
                json={"results": results, "has_more": False},
            )
        create_calls += 1
        return httpx.Response(503, json={"message": "accepted but unavailable"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(RuntimeError, match="Multiple Notion pages"):
            await client._ensure_month("July", NotionCache())
    finally:
        await client.aclose()
    assert create_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "cache_field", "title"),
    [
        ("_ensure_month", "months", "July"),
        ("_ensure_year", "years", "2026"),
    ],
)
async def test_relation_preflight_prevents_second_create_after_failed_reconcile(
    method, cache_field, title
):
    client = NotionClient.__new__(NotionClient)
    client._headers = {}
    client._db_ids = {"months_ds": "months-db", "years_ds": "years-db"}
    create_calls = 0
    query_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls, query_calls
        if request.url.path.endswith("/query"):
            query_calls += 1
            if query_calls == 2:
                # The immediate post-create reconciliation is unavailable.
                return httpx.Response(400, json={"message": "temporary lookup failure"})
            results = [] if query_calls == 1 else [_title_page(title, "relation-1")]
            return httpx.Response(200, json={"results": results, "has_more": False})
        create_calls += 1
        return httpx.Response(503, json={"message": "accepted but unavailable"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = NotionCache()
    try:
        assert await getattr(client, method)(title, cache) is None
        result = await getattr(client, method)(title, cache)
    finally:
        await client.aclose()

    assert result == "https://notion.so/relation-1"
    assert getattr(cache, cache_field)[title] == result
    assert create_calls == 1
    assert query_calls == 3


@pytest.mark.asyncio
async def test_relation_preflight_lookup_failure_never_posts_create():
    client = NotionClient.__new__(NotionClient)
    client._headers = {}
    client._db_ids = {"months_ds": "months-db"}
    create_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_calls
        if request.url.path.endswith("/query"):
            return httpx.Response(400, json={"message": "lookup unavailable"})
        create_calls += 1
        return httpx.Response(201, json={"url": PAGE_URL})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client._ensure_month("July", NotionCache()) is None
    finally:
        await client.aclose()
    assert create_calls == 0


@pytest.mark.asyncio
async def test_existing_notion_page_is_updated_without_recreating_it():
    """An existing UUID match is an update/upsert, never a second page create."""
    client = _client()
    updates = []
    creates = []

    async def query_db(_database_id, *, extra_payload=None):
        return [{"url": PAGE_URL, "id": "page-001"}]

    async def update_existing(page_url, *args, **kwargs):
        updates.append((page_url, args, kwargs))
        return PAGE_URL

    async def create_page(*args, **kwargs):
        creates.append((args, kwargs))
        return PAGE_URL

    client._query_db = query_db
    client._update_expense = update_existing
    client._notion_post = create_page

    result = await client.upsert_expense(
        _entry(),
        "Afif",
        NotionCache(),
        TRANSACTION_ID,
    )

    assert result == PAGE_URL
    assert updates, "the matched page should receive an update"
    assert creates == []


class _WorkerDB:
    def __init__(self, jobs):
        self.jobs = jobs
        self.failures = []
        self.successes = []

    async def list_due_notion_sync_jobs(self, now, limit):
        return self.jobs

    async def get_user(self, _user_id):
        return UserRecord(7, "Afif", "token")

    async def mark_notion_sync_failure(self, outbox_id, error, next_attempt_at):
        self.failures.append((outbox_id, error, next_attempt_at))

    async def mark_notion_sync_success(self, outbox_id, page_id):
        self.successes.append((outbox_id, page_id))


class _RecurringClient:
    def __init__(self):
        self.calls = []

    async def load_cache(self):
        return object()

    async def upsert_expense(self, entry, owner, cache, transaction_id, *, recurring_page_url=None):
        self.calls.append((transaction_id, recurring_page_url))
        return PAGE_URL

    async def aclose(self):
        return None


def _job(outbox_id, *, recurring_page_url=None):
    job = {
        "outbox_id": outbox_id,
        "operation": "upsert",
        "attempt_count": 0,
        "transaction_id": f"tx-{outbox_id}",
        "user_id": 7,
        "kind": "expense",
        "status": "confirmed",
        "amount_idr": 12000,
        "occurred_on": "2026-01-01",
        "description": "subscription",
        "merchant": "Service",
        "subcategory": "Other",
        "account": "Cash",
    }
    if recurring_page_url is not None:
        job["recurring_page_url"] = recurring_page_url
    return job


@pytest.mark.asyncio
async def test_recurring_relation_metadata_reaches_notion_worker():
    db = _WorkerDB([_job(1, recurring_page_url="https://notion.so/recurring-1")])
    client = _RecurringClient()
    worker = NotionSyncWorker(db, client_factory=lambda _user: client, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    await worker.run_once()

    assert client.calls == [("tx-1", "https://notion.so/recurring-1")]


@pytest.mark.asyncio
async def test_one_job_failure_does_not_prevent_other_jobs_from_running():
    jobs = [_job(1), _job(2)]
    db = _WorkerDB(jobs)
    healthy = _RecurringClient()
    calls = 0

    class FlakyClient(_RecurringClient):
        async def upsert_expense(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("first job failed")
            return await healthy.upsert_expense(*args, **kwargs)

    worker = NotionSyncWorker(db, client_factory=lambda _user: FlakyClient(), clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), jitter=lambda _a, _b: 0)

    assert await worker.run_once() == 2
    assert len(db.failures) == 1
    assert len(healthy.calls) == 1
