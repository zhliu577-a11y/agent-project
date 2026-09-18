# tests/test_session_gateway.py —— 会话网关：JSONL 存储往返与多轮历史延续
from pathlib import Path

import pytest

from core.compaction import CompactionRequest, CompactionResult
from core.hooks import HookGateway
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.session import SessionConflictError, SessionMetadata, SessionStore
from core.types import Message, ModelResponse, ToolCall
from gateways.session_gateway import SessionGateway
from loop import run_agent
from plugins.loader import load_session_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _jsonl_gateway(tmp_path: Path, monkeypatch) -> SessionGateway:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path))
    return SessionGateway(plugin.create(), session_id="unit")


def _sample_history() -> list[Message]:
    return [
        Message(role="user", content="现在几点"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="1", name="time__get_current_time", arguments={})],
        ),
        Message(role="tool", content="12:00", tool_call_id="1"),
        Message(role="assistant", content="现在是 12:00"),
    ]


@pytest.mark.asyncio
async def test_jsonl_store_roundtrip(tmp_path, monkeypatch) -> None:
    gateway = _jsonl_gateway(tmp_path, monkeypatch)
    history = _sample_history()

    await gateway.save_history(history)
    loaded = await gateway.load_history()

    assert loaded == history
    assert loaded[1].tool_calls[0].name == "time__get_current_time"
    assert loaded[3].content == "现在是 12:00"


@pytest.mark.asyncio
async def test_jsonl_store_missing_session_returns_empty(tmp_path, monkeypatch) -> None:
    gateway = _jsonl_gateway(tmp_path, monkeypatch)
    assert await gateway.load_history() == []


@pytest.mark.asyncio
async def test_jsonl_store_append_limit_metadata_and_conflict(tmp_path, monkeypatch) -> None:
    gateway = _jsonl_gateway(tmp_path, monkeypatch)
    history = _sample_history()

    first = await gateway.append_messages(history[:2])
    assert first.revision == 1
    assert first.message_count == 2
    assert [message.content for message in await gateway.load_history(limit=1)] == [
        history[1].content
    ]

    stale = _jsonl_gateway(tmp_path, monkeypatch)
    assert len(await stale.load_history()) == 2
    await gateway.append_messages(history[2:])
    with pytest.raises(SessionConflictError, match="session revision conflict"):
        await stale.save_history(history)


@pytest.mark.asyncio
async def test_jsonl_store_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path))
    store = plugin.create()

    with pytest.raises(ValueError, match="path separators"):
        await store.load("../outside")
    with pytest.raises(ValueError, match="path separators"):
        SessionGateway(store, session_id="..\\outside")
    with pytest.raises(ValueError, match="reserved filename"):
        SessionGateway(store, session_id="bad:name")


@pytest.mark.asyncio
async def test_gateway_commit_turn_compacts_and_writes_checkpoint(tmp_path, monkeypatch) -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path))

    class CompactOld:
        async def compact(self, request: CompactionRequest) -> CompactionResult:
            summary = Message(
                role="system",
                content="Earlier conversation summary: old context",
                metadata={"compaction": "summary"},
            )
            return CompactionResult(
                messages=[summary, request.messages[-1]],
                compacted=True,
                summary=summary.content,
            )

    gateway = SessionGateway(
        plugin.create(),
        session_id="unit",
        compaction=CompactOld(),
        max_tokens=100,
    )
    committed = await gateway.commit_turn(
        _sample_history(),
        {"turn": 1, "stop_reason": "done"},
    )

    assert len(committed.messages) == 2
    assert committed.messages[0].metadata["compaction"] == "summary"
    assert committed.metadata.revision == 1
    checkpoint = await gateway.load_checkpoint()
    assert checkpoint is not None
    assert checkpoint["session_revision"] == 1
    assert checkpoint["message_count"] == 2


@pytest.mark.asyncio
async def test_gateway_clear_removes_history_and_checkpoint(tmp_path, monkeypatch) -> None:
    gateway = _jsonl_gateway(tmp_path, monkeypatch)
    await gateway.save_history(_sample_history())
    await gateway.save_checkpoint({"turn": 1})

    metadata = await gateway.clear()

    assert metadata.message_count == 0
    assert await gateway.load_history() == []
    assert await gateway.load_checkpoint() is None


@pytest.mark.asyncio
async def test_gateway_supports_legacy_store_without_revisions() -> None:
    class LegacyStore(SessionStore):
        def __init__(self) -> None:
            self.messages: list[Message] = []

        async def load(self, session_id):
            return list(self.messages)

        async def save(self, session_id, messages):
            self.messages = list(messages)

        async def load_checkpoint(self, session_id):
            return None

        async def save_checkpoint(self, session_id, snapshot):
            return None

        async def delete_checkpoint(self, session_id):
            return None

    gateway = SessionGateway(LegacyStore(), session_id="legacy")
    first = await gateway.commit_turn([Message(role="user", content="one")])
    second = await gateway.commit_turn([Message(role="user", content="two")])

    assert first.metadata.revision == 1
    assert second.metadata.revision == 2


class FakeModel(ModelAdapter):
    """记录每次收到的消息，固定返回纯文本。"""

    def __init__(self) -> None:
        self.seen: list[list[Message]] = []

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        self.seen.append(list(messages))
        return ModelResponse(content="收到", tool_calls=[])


@pytest.mark.asyncio
async def test_run_agent_carries_history_between_turns() -> None:
    model = FakeModel()
    tools = ToolRegistry()
    hooks = HookGateway()

    first = await run_agent(model, tools, hooks, "系统", "第一问")
    history = first.messages[1:]  # 去掉 system，模拟 chat() 的持久化裁剪
    second = await run_agent(model, tools, hooks, "系统", "第二问", history=history)

    assert len(model.seen) == 2
    second_prompt = model.seen[1]
    roles = [m.role for m in second_prompt]
    # system → 第一轮历史(user/assistant) → 新 user
    assert roles == ["system", "user", "assistant", "user"]
    assert second_prompt[-1].content == "第二问"
    assert second.messages[-1].content == "收到"


def test_repo_offers_swappable_session_backends() -> None:
    names = {plugin.manifest.name for plugin in load_session_plugins(REPO_PLUGINS)}
    assert {"jsonl", "inmemory"} <= names


@pytest.mark.asyncio
async def test_inmemory_backend_roundtrip_within_process() -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "inmemory")
    store = plugin.create()
    gateway = SessionGateway(store, session_id="unit")
    history = _sample_history()

    await gateway.save_history(history)
    loaded = await gateway.load_history()
    assert loaded == history

    # 换个网关实例指向同一个 store，仍能读回（证明状态在 store 里，不在网关里）
    another = SessionGateway(store, session_id="unit")
    assert await another.load_history() == history


@pytest.mark.asyncio
async def test_jsonl_checkpoint_roundtrip_and_delete(tmp_path, monkeypatch) -> None:
    gateway = _jsonl_gateway(tmp_path, monkeypatch)
    assert await gateway.load_checkpoint() is None

    snapshot = {"turn": 2, "stop_reason": "done", "state": {"count": 1}}
    await gateway.save_checkpoint(snapshot)
    assert await gateway.load_checkpoint() == snapshot

    await gateway.delete_checkpoint()
    assert await gateway.load_checkpoint() is None


@pytest.mark.asyncio
async def test_jsonl_store_lists_and_deletes_sessions(tmp_path, monkeypatch) -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "jsonl")
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path))
    store = plugin.create()

    await store.save_metadata("one", SessionMetadata(title="One"))
    await store.save_metadata("two", SessionMetadata(title="Two"))
    await SessionGateway(store, session_id="one").append_messages(_sample_history())

    sessions = dict(await store.list_sessions())
    assert set(sessions) == {"one", "two"}
    assert sessions["one"].message_count == 4
    assert sessions["two"].title == "Two"

    await store.delete("one")
    assert {session_id for session_id, _metadata in await store.list_sessions()} == {"two"}


@pytest.mark.asyncio
async def test_inmemory_checkpoint_roundtrip() -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "inmemory")
    gateway = SessionGateway(plugin.create(), session_id="unit")
    snapshot = {"turn": 1, "stop_reason": "max_turns", "state": {"x": [1, 2]}}
    await gateway.save_checkpoint(snapshot)
    assert await gateway.load_checkpoint() == snapshot
    await gateway.delete_checkpoint()
    assert await gateway.load_checkpoint() is None
