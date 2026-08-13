import pytest

from db import Database
from reconciliation import reconcile_transactions


class FakeNotionClient:
    def __init__(self, expenses, income):
        self._db_ids = {"expenses_ds": "expenses", "income_ds": "income"}
        self._pages = {"expenses": expenses, "income": income}
        self.queries = []

    async def _query_db(self, database_id, *, extra_payload=None):
        assert extra_payload is None
        self.queries.append(database_id)
        return self._pages[database_id]


def notion_page(page_id, transaction_id=None, *, archived=False):
    rich_text = []
    if transaction_id is not None:
        rich_text = [{"plain_text": transaction_id}]
    return {
        "id": page_id,
        "archived": archived,
        "properties": {"Transaction ID": {"rich_text": rich_text}},
    }


async def create_transaction(db, user_id, source_ref, *, kind="expense"):
    transaction, created = await db.create_confirmed_external_transaction(
        user_id,
        kind=kind,
        amount_idr=10_000,
        occurred_on="2026-07-29",
        description="Test transaction",
        source="test",
        source_ref=source_ref,
    )
    assert created
    return transaction


@pytest.mark.asyncio
async def test_reconciliation_reports_all_requested_differences_without_writing(tmp_path):
    db = await Database.connect(str(tmp_path / "reconciliation.db"))
    try:
        mismatched = await create_transaction(db, 7, "mismatched")
        await db.mark_notion_sync_success(mismatched["id"], "persisted-page")

        missing = await create_transaction(db, 7, "missing", kind="income")
        wrong_kind = await create_transaction(db, 7, "wrong-kind", kind="income")

        duplicated = await create_transaction(db, 7, "duplicated")

        voided = await create_transaction(db, 7, "voided")
        await db.mark_notion_sync_success(voided["id"], "void-active")
        await db.void_transaction(7, voided["id"])

        # This other user's record must not affect user 7's result.
        other_user = await create_transaction(db, 8, "other-user")

        client = FakeNotionClient(
            expenses=[
                notion_page("remote-page", mismatched["id"]),
                notion_page("duplicate-one", duplicated["id"]),
                notion_page("duplicate-two", duplicated["id"]),
                notion_page("void-active", voided["id"]),
                notion_page("wrong-kind-page", wrong_kind["id"]),
                notion_page("unexpected", "not-a-local-id"),
                notion_page("no-transaction-id"),
                notion_page("archived-page", other_user["id"], archived=True),
            ],
            income=[],
        )

        before_events = await (
            await db._conn.execute("SELECT COUNT(*) FROM transaction_events")
        ).fetchone()
        report = await reconcile_transactions(db, client, 7)
        after_events = await (
            await db._conn.execute("SELECT COUNT(*) FROM transaction_events")
        ).fetchone()

        assert client.queries == ["expenses", "income"]
        assert [row.transaction_id for row in report.missing_remote] == [missing["id"]]
        assert {row.transaction_id for row in report.unexpected_remote} == {
            "not-a-local-id",
            None,
        }
        assert list(report.duplicate_ids) == [duplicated["id"]]
        assert [row.page_id for row in report.duplicate_ids[duplicated["id"]]] == [
            "duplicate-one",
            "duplicate-two",
        ]
        assert [row.transaction_id for row in report.notion_page_id_mismatches] == [
            mismatched["id"]
        ]
        assert [row.transaction_id for row in report.kind_mismatches] == [
            wrong_kind["id"]
        ]
        assert [row.transaction_id for row in report.voided_pages_still_active] == [
            voided["id"]
        ]
        assert report.is_clean is False
        assert before_events[0] == after_events[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconciliation_accepts_dashless_page_ids_and_ignores_pending_rows(tmp_path):
    db = await Database.connect(str(tmp_path / "reconciliation.db"))
    try:
        confirmed = await create_transaction(db, 7, "confirmed")
        local_page_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        await db.mark_notion_sync_success(confirmed["id"], local_page_id)
        await db.create_ingested_transaction(
            7,
            kind="expense",
            amount_idr=20_000,
            occurred_on="2026-07-29",
            description="Pending",
            source_ref="pending",
        )
        client = FakeNotionClient(
            expenses=[
                notion_page(
                    "aaaaaaaabbbbccccddddeeeeeeeeeeee", confirmed["id"]
                )
            ],
            income=[],
        )

        report = await reconcile_transactions(db, client, 7)

        assert report.is_clean
    finally:
        await db.close()
