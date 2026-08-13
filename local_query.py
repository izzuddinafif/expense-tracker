"""Application service for Notion-independent conversational ledger queries."""

from __future__ import annotations

from typing import Any


class LocalQueryService:
    def __init__(self, database: Any, reporting: Any, agent: Any) -> None:
        self._db = database
        self._reporting = reporting
        self._agent = agent

    async def answer(self, user_id: int, text: str, owner: str) -> str:
        """Answer from confirmed SQLite expenses and persist successful history."""
        expenses = await self._reporting.expense_context(user_id)
        history = await self._db.get_history(user_id)
        answer, _ = await self._agent.answer_query(
            text,
            expenses,
            owner,
            history,
            assets=None,
        )
        await self._db.append_history(user_id, "user", text)
        await self._db.append_history(user_id, "assistant", answer)
        return answer
