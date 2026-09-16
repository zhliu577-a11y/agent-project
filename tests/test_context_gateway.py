from typing import Any

import pytest

from core.context import ContextRequest, ContextResult, MemoryTailWindowPolicy
from core.memory import MemoryRecallPort
from core.types import Message
from gateways.context_gateway import ContextGateway


class RecordingPolicy:
    def __init__(self) -> None:
        self.requests: list[ContextRequest] = []

    async def prepare(self, request: ContextRequest) -> ContextResult:
        self.requests.append(request)
        return ContextResult(messages=list(request.messages), metadata=dict(request.state))


class FakeMemory:
    def __init__(self) -> None:
        self.queries: list[tuple[str, str, str]] = []

    async def context_records(
        self,
        query: str,
        *,
        scope: str,
        owner_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.queries.append((query, scope, owner_id))
        return [{"id": "m1", "content": "remembered", "limit": str(limit)}]


class FailingMemory:
    def __init__(self) -> None:
        self.calls = 0

    async def context_records(
        self,
        query: str,
        *,
        scope: str,
        owner_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.calls += 1
        raise RuntimeError("memory unavailable")


@pytest.mark.asyncio
async def test_context_gateway_exposes_recall_results_to_policy() -> None:
    policy = RecordingPolicy()
    memory = FakeMemory()
    gateway = ContextGateway(policy, memory=memory)
    request = ContextRequest(
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="what did we decide?"),
        ],
        tools=[],
        max_tokens=1000,
        session_id="test",
        turn=0,
    )

    result = await gateway.prepare(request)

    assert memory.queries == [("what did we decide?", "user", "")]
    assert result.metadata["memory.records"][0]["id"] == "m1"
    assert policy.requests[0].state["memory.scope"] == "user"


@pytest.mark.asyncio
async def test_context_gateway_degrades_when_memory_recall_fails() -> None:
    policy = RecordingPolicy()
    memory = FailingMemory()
    gateway = ContextGateway(policy, memory=memory)
    request = ContextRequest(
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="what did we decide?"),
        ],
        tools=[],
        max_tokens=1000,
        session_id="test",
        turn=0,
    )

    result = await gateway.prepare(request)
    repeated = await gateway.prepare(request)

    assert result.messages == request.messages
    assert "memory.records" not in result.metadata
    assert repeated.messages == request.messages
    assert memory.calls == 1
    assert policy.requests == [request, request]


def test_memory_recall_port_is_structural() -> None:
    assert isinstance(FakeMemory(), MemoryRecallPort)


@pytest.mark.asyncio
async def test_context_gateway_closes_automatic_memory_injection_loop() -> None:
    gateway = ContextGateway(MemoryTailWindowPolicy(), memory=FakeMemory())
    request = ContextRequest(
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="what did we decide?"),
        ],
        tools=[],
        max_tokens=1000,
        session_id="test",
        turn=0,
    )

    result = await gateway.prepare(request)

    assert result.metadata["strategy"] == "memory-tail-window"
    assert "remembered" in result.messages[0].content
