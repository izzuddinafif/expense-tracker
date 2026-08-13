from types import SimpleNamespace

import pytest

from email_watcher import EmailWatcher


class ReportingStub:
    def __init__(self):
        self.calls = []

    async def duplicate_descriptions(
        self, user_id, amount, occurred_on, *, kind="expense"
    ):
        self.calls.append(("duplicates", user_id, amount, occurred_on, kind))
        return [f"{kind} candidate"]

    async def similar_by_merchant(
        self, user_id, merchant, amount, occurred_on
    ):
        self.calls.append(
            ("merchant", user_id, merchant, amount, occurred_on)
        )
        return [{"amount": amount, "date": occurred_on}]


class NotionMustNotBeCalled:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected Notion call: {name}")


@pytest.mark.asyncio
async def test_email_duplicate_and_merchant_context_use_local_reporting():
    reporting = ReportingStub()
    watcher = EmailWatcher(
        config=SimpleNamespace(),
        db=object(),
        notion=NotionMustNotBeCalled(),
        agent=None,
        cache_getter=lambda: None,
        reporting=reporting,
    )
    notion = NotionMustNotBeCalled()

    expense = await watcher._duplicate_descriptions(
        7, notion, "Afif", 25_000.0, "2026-07-29"
    )
    income = await watcher._duplicate_descriptions(
        7,
        notion,
        "Afif",
        25_000.0,
        "2026-07-29",
        kind="income",
    )
    similar = await watcher._similar_by_merchant(
        7,
        notion,
        "Afif",
        "Warung",
        25_000.0,
        "2026-07-29",
        None,
    )

    assert expense == ["expense candidate"]
    assert income == ["income candidate"]
    assert similar == [{"amount": 25_000.0, "date": "2026-07-29"}]
    assert reporting.calls == [
        ("duplicates", 7, 25_000.0, "2026-07-29", "expense"),
        ("duplicates", 7, 25_000.0, "2026-07-29", "income"),
        ("merchant", 7, "Warung", 25_000.0, "2026-07-29"),
    ]


@pytest.mark.asyncio
async def test_local_reporting_failure_propagates_to_callers():
    class FailingReporting(ReportingStub):
        async def duplicate_descriptions(self, *args, **kwargs):
            raise RuntimeError("SQLite unavailable")

    watcher = EmailWatcher(
        config=SimpleNamespace(),
        db=object(),
        notion=NotionMustNotBeCalled(),
        agent=None,
        cache_getter=lambda: None,
        reporting=FailingReporting(),
    )

    with pytest.raises(RuntimeError, match="SQLite unavailable"):
        await watcher._duplicate_descriptions(
            7,
            NotionMustNotBeCalled(),
            "Afif",
            25_000.0,
            "2026-07-29",
        )
