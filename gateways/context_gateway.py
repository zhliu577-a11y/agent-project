"""Context assembly boundary for one model call."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from core.context import ContextPolicy, ContextRequest, ContextResult
from core.memory import MemoryRecallPort

logger = logging.getLogger(__name__)


class ContextGateway:
    """Enrich a context request with recall data, then invoke a policy plugin."""

    def __init__(
        self,
        policy: ContextPolicy,
        *,
        memory: MemoryRecallPort | None = None,
        default_scope: str = "user",
        default_owner_id: str = "",
        recall_limit: int = 8,
    ) -> None:
        if recall_limit <= 0:
            raise ValueError("recall_limit must be positive")
        self._policy = policy
        self._memory = memory
        self._default_scope = default_scope
        self._default_owner_id = default_owner_id
        self._recall_limit = recall_limit
        self._cache_key: tuple[str | None, int, str] | None = None
        self._cache_records: list[dict[str, Any]] = []

    def _latest_user_text(self, request: ContextRequest) -> str:
        for message in reversed(request.messages):
            if message.role == "user" and message.content.strip():
                return message.content.strip()
        return ""

    async def _recall(self, request: ContextRequest, query: str) -> list[dict[str, Any]]:
        if self._memory is None or not query:
            return []
        key = (request.session_id, request.turn, query)
        if self._cache_key == key:
            return list(self._cache_records)
        try:
            records = await self._memory.context_records(
                query,
                scope=self._default_scope,
                owner_id=self._default_owner_id,
                limit=self._recall_limit,
            )
        except Exception as exc:
            logger.warning("memory context recall failed; continuing without memory: %s", exc)
            self._cache_key = key
            self._cache_records = []
            return []
        self._cache_key = key
        self._cache_records = records
        return list(records)

    async def prepare(self, request: ContextRequest) -> ContextResult:
        query = self._latest_user_text(request)
        records = await self._recall(request, query)
        if not records:
            return await self._policy.prepare(request)

        state = dict(request.state)
        state["memory.records"] = records
        state["memory.scope"] = self._default_scope
        state["memory.owner_id"] = self._default_owner_id
        return await self._policy.prepare(replace(request, state=state))
