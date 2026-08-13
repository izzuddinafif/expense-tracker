import pytest

from db import Database
from models import NotionCache
from reference_store import ReferenceStore, load_resilient_cache


def full_cache(label: str) -> NotionCache:
    return NotionCache(
        subcategories={f"Dining {label}": "sub-url"},
        accounts={f"Jago {label}": "account-url"},
        months={"July": "month-url"},
        years={"2026": "year-url"},
        income_subcategories={"Salary": "income-sub-url"},
        income_months={"July": "income-month-url"},
        income_years={"2026": "income-year-url"},
        category_subcategories={"Food": [f"Dining {label}", "Groceries"]},
        recurring_payments={
            99_000: [
                {
                    "name": "Internet",
                    "page_url": "recurring-url",
                    "subcategory": "Utilities",
                    "account": f"Jago {label}",
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_reference_snapshot_round_trip_and_integer_recurring_keys(tmp_path):
    db = await Database.connect(str(tmp_path / "references.db"))
    store = ReferenceStore(db)
    try:
        await store.save(7, full_cache("primary"))
        loaded = await store.load(7)
        assert loaded == full_cache("primary")
        assert list(loaded.recurring_payments) == [99_000]
        assert isinstance(next(iter(loaded.recurring_payments)), int)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reference_snapshot_is_user_scoped_and_replaced(tmp_path):
    db = await Database.connect(str(tmp_path / "references.db"))
    store = ReferenceStore(db)
    try:
        await store.save(7, full_cache("old"))
        await store.save(8, full_cache("other"))
        await store.save(7, full_cache("new"))
        assert (await store.load(7)).accounts == {"Jago new": "account-url"}
        assert (await store.load(8)).accounts == {"Jago other": "account-url"}
        assert await store.load(9) is None
        count = await (
            await db._conn.execute("SELECT COUNT(*) FROM notion_cache_snapshots")
        ).fetchone()
        assert count[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"accounts":[]}',
        '{"recurring_payments":{"not-an-int":[]}}',
    ],
)
async def test_malformed_reference_snapshot_is_ignored(tmp_path, payload):
    db = await Database.connect(str(tmp_path / "references.db"))
    store = ReferenceStore(db)
    try:
        await db._conn.execute(
            "INSERT INTO notion_cache_snapshots(user_id,cache_json,refreshed_at) "
            "VALUES (7,?,'2026-07-29T00:00:00+00:00')",
            (payload,),
        )
        await db._conn.commit()
        assert await store.load(7) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resilient_loader_persists_remote_and_uses_snapshot_on_failure(tmp_path):
    db = await Database.connect(str(tmp_path / "references.db"))
    store = ReferenceStore(db)
    try:
        async def remote():
            return full_cache("remote")

        loaded = await load_resilient_cache(store, 7, remote, timeout=1)
        assert loaded.source == "remote"
        assert loaded.error is None
        assert (await store.load(7)).accounts == {"Jago remote": "account-url"}

        async def unavailable():
            raise RuntimeError("Notion unavailable")

        fallback = await load_resilient_cache(store, 7, unavailable, timeout=1)
        assert fallback.source == "snapshot"
        assert isinstance(fallback.error, RuntimeError)
        assert fallback.cache.accounts == {"Jago remote": "account-url"}

        empty = await load_resilient_cache(store, 8, unavailable, timeout=1)
        assert empty.source == "empty"
        assert empty.cache == NotionCache()

        called = False

        async def must_not_call():
            nonlocal called
            called = True
            raise AssertionError("remote loader should not run")

        immediate = await load_resilient_cache(
            store,
            7,
            must_not_call,
            timeout=1,
            prefer_snapshot=True,
        )
        assert immediate.source == "snapshot"
        assert immediate.cache.accounts == {"Jago remote": "account-url"}
        assert called is False
    finally:
        await db.close()
