import pytest

from models import UserRecord
from notion import NotionClient
from notion_sync import NotionSyncWorker


class _DB:
    def __init__(self, job):
        self.job = job
        self.successes = []
        self.failures = []

    async def list_due_notion_sync_jobs(self, _now, limit):
        if self.successes:
            return []
        return [self.job][:limit]

    async def get_user(self, _user_id):
        return UserRecord(1, "Afif", "token")

    async def mark_notion_sync_success(self, outbox_id, page_id=None):
        self.successes.append((outbox_id, page_id))

    async def mark_notion_sync_failure(self, outbox_id, error, next_attempt_at):
        self.failures.append((outbox_id, error, next_attempt_at))


def _job(**extra):
    job = {
        "outbox_id": 4,
        "operation": "archive",
        "attempt_count": 0,
        "transaction_id": "tx-4",
        "user_id": 1,
        "kind": "expense",
        "status": "voided",
        "notion_page_id": None,
    }
    job.update(extra)
    return job


@pytest.mark.asyncio
async def test_archive_uses_persisted_page_id_and_is_idempotent():
    class Client:
        def __init__(self):
            self.calls = []

        async def archive_transaction(self, kind, transaction_id, page_id):
            self.calls.append((kind, transaction_id, page_id))
            return page_id

        async def aclose(self):
            pass

    client = Client()
    db = _DB(_job(notion_page_id="page-4"))
    worker = NotionSyncWorker(db, client_factory=lambda _user: client)
    await worker.run_once()
    await worker.run_once()
    assert client.calls == [("expense", "tx-4", "page-4")]
    assert db.successes == [(4, "page-4")]


@pytest.mark.asyncio
async def test_archive_transaction_resolves_transaction_id_when_page_id_missing():
    client = NotionClient.__new__(NotionClient)
    client._db_ids = {"expenses_ds": "expenses-db"}
    looked_up = []
    archived = []

    async def find(db_id, transaction_id):
        looked_up.append((db_id, transaction_id))
        return ("https://notion.so/page-4", True)

    async def archive(page_id):
        archived.append(page_id)

    client._find_by_transaction_id = find
    client.archive_page = archive
    assert await client.archive_transaction("expense", "tx-4") == "page-4"
    assert looked_up == [("expenses-db", "tx-4")]
    assert archived == ["https://notion.so/page-4"]


@pytest.mark.asyncio
async def test_archive_transaction_missing_page_is_successful_noop():
    client = NotionClient.__new__(NotionClient)
    client._db_ids = {"income_ds": "income-db"}

    async def find(_db_id, _transaction_id):
        return (None, True)

    client._find_by_transaction_id = find
    assert await client.archive_transaction("income", "tx-missing") is None
