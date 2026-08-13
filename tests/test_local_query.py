import pytest

from local_query import LocalQueryService


class FakeDatabase:
    def __init__(self):
        self.writes = []

    async def get_history(self, user_id):
        assert user_id == 7
        return [{"role": "user", "content": "Earlier question"}]

    async def append_history(self, user_id, role, content):
        self.writes.append((user_id, role, content))


class FakeReporting:
    async def expense_context(self, user_id):
        assert user_id == 7
        return [{"description": "Lunch", "amount": 25_000}]


class FakeAgent:
    async def answer_query(self, text, expenses, owner, history, assets):
        assert text == "How much was lunch?"
        assert expenses == [{"description": "Lunch", "amount": 25_000}]
        assert owner == "Afif"
        assert history == [{"role": "user", "content": "Earlier question"}]
        assert assets is None
        return "Rp 25.000", history


@pytest.mark.asyncio
async def test_local_query_uses_sqlite_context_without_assets():
    db = FakeDatabase()
    service = LocalQueryService(db, FakeReporting(), FakeAgent())

    answer = await service.answer(7, "How much was lunch?", "Afif")

    assert answer == "Rp 25.000"
    assert db.writes == [
        (7, "user", "How much was lunch?"),
        (7, "assistant", "Rp 25.000"),
    ]


@pytest.mark.asyncio
async def test_local_query_does_not_write_partial_history_on_failure():
    class FailingAgent(FakeAgent):
        async def answer_query(self, *args, **kwargs):
            raise RuntimeError("provider unavailable")

    db = FakeDatabase()
    service = LocalQueryService(db, FakeReporting(), FailingAgent())

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service.answer(7, "How much was lunch?", "Afif")

    assert db.writes == []
