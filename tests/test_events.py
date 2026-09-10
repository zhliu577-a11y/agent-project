# tests/test_events.py —— 事件总线：顺序、折叠、追踪、落盘与桥接
import json

import pytest

from core.events import Event, EventBus, jsonl_sink
from core.hooks import HookGateway, LifecycleHooks
from core.memory import MemoryNote, MemoryStore
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.tracing import begin_trace
from core.types import ModelResponse, ToolCall
from gateways.memory_gateway import MemoryGateway
from loop import run_agent


@pytest.mark.asyncio
async def test_publish_respects_priority_and_supports_sync_handlers() -> None:
    order: list[str] = []
    bus = EventBus()
    bus.subscribe("demo", lambda event: order.append("sync"), priority=100)
    bus.subscribe("demo", lambda event: order.append("late"), priority=50)

    async def async_handler(event) -> None:
        order.append("early")

    bus.subscribe("demo", async_handler, priority=10)
    await bus.publish(Event("demo"))

    assert order == ["early", "late", "sync"]


@pytest.mark.asyncio
async def test_publish_isolates_handler_exceptions() -> None:
    seen: list[str] = []
    bus = EventBus()

    def boom(event) -> None:
        raise RuntimeError("handler 崩了")

    bus.subscribe("demo", boom, priority=1)
    bus.subscribe("demo", lambda event: seen.append("after"))

    await bus.publish(Event("demo"))  # 不抛出
    assert seen == ["after"]


@pytest.mark.asyncio
async def test_wildcard_subscription_receives_all_events() -> None:
    names: list[str] = []
    bus = EventBus()
    bus.subscribe("*", lambda event: names.append(event.name))
    await bus.publish(Event("a"))
    await bus.publish(Event("b"))
    assert names == ["a", "b"]


@pytest.mark.asyncio
async def test_decide_folds_deny_over_ask_over_allow() -> None:
    bus = EventBus()
    bus.subscribe("gate", lambda event: None, priority=1)  # 弃权
    bus.subscribe("gate", lambda event: "allow", priority=2)
    bus.subscribe("gate", lambda event: "ask", priority=3)
    assert await bus.decide(Event("gate")) == "ask"

    bus.subscribe("gate", lambda event: "deny", priority=4)
    assert await bus.decide(Event("gate")) == "deny"


@pytest.mark.asyncio
async def test_decide_treats_exception_and_invalid_vote_as_deny() -> None:
    bus = EventBus()

    def boom(event) -> str:
        raise RuntimeError("坏订阅者")

    bus.subscribe("gate", boom)
    assert await bus.decide(Event("gate")) == "deny"

    other = EventBus()
    other.subscribe("gate", lambda event: "maybe")
    assert await other.decide(Event("gate")) == "deny"


@pytest.mark.asyncio
async def test_event_gets_trace_id_from_context() -> None:
    seen: list[str | None] = []
    bus = EventBus()
    bus.subscribe("demo", lambda event: seen.append(event.trace_id))

    begin_trace("trace-x")
    await bus.publish(Event("demo"))
    assert seen == ["trace-x"]


@pytest.mark.asyncio
async def test_jsonl_sink_writes_safe_payload(tmp_path) -> None:
    log_path = tmp_path / "events.jsonl"
    bus = EventBus()
    bus.subscribe("*", jsonl_sink(log_path))

    await bus.publish(Event("demo", {"text": "你好", "obj": object()}))

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "demo"
    assert record["payload"]["text"] == "你好"
    assert isinstance(record["payload"]["obj"], str)


class RecordingHooks(LifecycleHooks):
    def __init__(self) -> None:
        self.turn_starts = 0
        self.llm_responses = 0
        self.tool_afters = 0
        self.turn_ends = 0

    async def turn_start(self, ctx) -> None:
        self.turn_starts += 1

    async def llm_response(self, ctx, resp) -> None:
        self.llm_responses += 1

    async def tool_after(self, ctx, tool_call, result, ok) -> None:
        self.tool_afters += 1

    async def turn_end(self, ctx) -> None:
        self.turn_ends += 1


@pytest.mark.asyncio
async def test_hook_gateway_bridges_observation_events() -> None:
    hooks = HookGateway()
    recording = RecordingHooks()
    hooks.add(recording)
    bus = EventBus()
    hooks.attach(bus)

    await bus.publish(Event("turn.start", {"ctx": None}))
    await bus.publish(Event("model.response", {"ctx": None, "resp": ModelResponse("", [])}))
    await bus.publish(
        Event("tool.after", {"ctx": None, "tool_call": None, "result": "x", "ok": True})
    )
    await bus.publish(Event("turn.end", {"ctx": None}))

    assert (recording.turn_starts, recording.llm_responses) == (1, 1)
    assert (recording.tool_afters, recording.turn_ends) == (1, 1)


class EchoTool(Tool):
    name = "echo"
    description = "回显"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "echo-ok"


class ScriptModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_loop_publishes_events_and_still_runs_hooks() -> None:
    names: list[str] = []
    bus = EventBus()
    bus.subscribe("*", lambda event: names.append(event.name))

    hooks = HookGateway()
    recording = RecordingHooks()
    hooks.add(recording)
    tools = ToolRegistry()
    tools.register(EchoTool())
    model = ScriptModel(
        [
            ModelResponse(content="", tool_calls=[ToolCall(id="1", name="echo", arguments={})]),
            ModelResponse(content="完成", tool_calls=[]),
        ]
    )

    ctx = await run_agent(model, tools, hooks, "系统", "执行", events=bus)

    assert ctx.stop_reason == "done"
    for expected in (
        "turn.start",
        "model.request",
        "model.response",
        "tool.start",
        "tool.after",
        "turn.end",
    ):
        assert expected in names
    # 旧 hook 插件通过总线桥接仍然被调用（且只调用一次）
    assert recording.turn_starts == 2  # 两次循环（工具轮 + 收尾轮）
    assert recording.llm_responses == 2
    assert recording.tool_afters == 1
    assert recording.turn_ends == 2


class QuickStore(MemoryStore):
    def __init__(self) -> None:
        self.notes: list[MemoryNote] = []

    async def list_notes(self) -> list[MemoryNote]:
        return list(self.notes)

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        note = MemoryNote(id=f"n{len(self.notes)}", content=content, tags=tags, created_at="now")
        self.notes.append(note)
        return note

    async def delete_note(self, note_id: str) -> bool:
        before = len(self.notes)
        self.notes = [note for note in self.notes if note.id != note_id]
        return len(self.notes) != before

    async def update_note(self, note_id, content=None, tags=None):
        for note in self.notes:
            if note.id == note_id:
                if content is not None:
                    note.content = content
                if tags is not None:
                    note.tags = tags
                return note
        return None

    async def search_notes(self, query: str) -> list[MemoryNote]:
        return list(self.notes)


@pytest.mark.asyncio
async def test_memory_gateway_publishes_write_events() -> None:
    names: list[str] = []
    bus = EventBus()
    bus.subscribe("*", lambda event: names.append(event.name))
    gateway = MemoryGateway(QuickStore(), events=bus)

    note = await gateway.remember("项目用 ruff", tags=["project"])
    await gateway.update(note.id, content="项目用 ruff（2026）")
    await gateway.forget(note.id)

    assert names == ["memory.write", "memory.update", "memory.delete"]
