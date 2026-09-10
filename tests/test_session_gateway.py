# tests/test_session_gateway.py —— 会话网关：JSONL 存储往返与多轮历史延续
from pathlib import Path

import pytest

from core.hooks import HookGateway
from core.model import ModelAdapter
from core.registry import ToolRegistry
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
async def test_inmemory_checkpoint_roundtrip() -> None:
    plugin = next(p for p in load_session_plugins(REPO_PLUGINS) if p.manifest.name == "inmemory")
    gateway = SessionGateway(plugin.create(), session_id="unit")
    snapshot = {"turn": 1, "stop_reason": "max_turns", "state": {"x": [1, 2]}}
    await gateway.save_checkpoint(snapshot)
    assert await gateway.load_checkpoint() == snapshot
    await gateway.delete_checkpoint()
    assert await gateway.load_checkpoint() is None
