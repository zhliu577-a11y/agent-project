# tests/test_events.py —— 事件总线：顺序、折叠、追踪、落盘与桥接
import asyncio
import json

import pytest

from core.events import (
    Event,
    EventBus,
    EventGateway,
    EventIdentity,
    EventScope,
    EventTransport,
    jsonl_sink,
)
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
    await bus.flush()

    assert order == ["early", "late", "sync"]
    await bus.stop()


@pytest.mark.asyncio
async def test_publish_isolates_handler_exceptions() -> None:
    seen: list[str] = []
    bus = EventBus()

    def boom(event) -> None:
        raise RuntimeError("handler 崩了")

    bus.subscribe("demo", boom, priority=1)
    bus.subscribe("demo", lambda event: seen.append("after"))

    await bus.publish(Event("demo"))  # 不抛出
    await bus.flush()
    assert seen == ["after"]
    await bus.stop()


@pytest.mark.asyncio
async def test_wildcard_subscription_receives_all_events() -> None:
    names: list[str] = []
    bus = EventBus()
    bus.subscribe("*", lambda event: names.append(event.name))
    await bus.publish(Event("a"))
    await bus.publish(Event("b"))
    await bus.flush()
    assert names == ["a", "b"]
    await bus.stop()


@pytest.mark.asyncio
async def test_subscription_scope_filters_run_and_session() -> None:
    names: list[str] = []
    bus = EventBus()
    bus.subscribe(
        "tool.after",
        lambda event: names.append(event.name),
        scope=EventScope(session_id="s1", run_id="r1"),
    )

    await bus.publish(Event("tool.after", session_id="s1", run_id="r2"))
    await bus.publish(Event("tool.after", session_id="s2", run_id="r1"))
    await bus.publish(Event("tool.after", session_id="s1", run_id="r1"))
    await bus.flush()

    assert names == ["tool.after"]
    await bus.stop()


@pytest.mark.asyncio
async def test_drop_newest_reports_overflow_without_blocking_publisher() -> None:
    seen: list[str] = []
    release = asyncio.Event()
    started = asyncio.Event()
    bus = EventBus(queue_size=1, overflow="drop_newest")

    async def blocking_handler(event) -> None:
        seen.append(event.payload["id"])
        started.set()
        await release.wait()

    bus.subscribe("demo", blocking_handler)
    await bus.publish(Event("demo", {"id": "one"}))
    await started.wait()
    await bus.publish(Event("demo", {"id": "two"}))
    await bus.publish(Event("demo", {"id": "three"}))

    assert bus.dropped_events == 1
    release.set()
    await bus.flush()
    assert seen == ["one", "two"]
    await bus.stop()


@pytest.mark.asyncio
async def test_gateway_is_observation_only() -> None:
    gateway = EventGateway()
    assert not hasattr(gateway, "decide")
    await gateway.stop()


@pytest.mark.asyncio
async def test_event_gets_trace_id_from_context() -> None:
    seen: list[str | None] = []
    bus = EventBus()
    bus.subscribe("demo", lambda event: seen.append(event.trace_id))

    begin_trace("trace-x")
    await bus.publish(Event("demo"))
    await bus.flush()
    assert seen == ["trace-x"]
    await bus.stop()


@pytest.mark.asyncio
async def test_gateway_binds_trusted_publisher_identity() -> None:
    gateway = EventGateway()
    trusted = EventIdentity.host("memory-gateway")
    seen = []
    gateway.subscribe("memory.write", lambda event: seen.append(event))

    await gateway.publisher(trusted).publish(
        Event(
            "memory.write",
            publisher=EventIdentity.host("forged-publisher"),
        )
    )
    await gateway.flush()

    assert seen[0].publisher == trusted
    assert seen[0].publisher.subject == "host:memory-gateway"
    await gateway.stop()


@pytest.mark.asyncio
async def test_subscriber_view_queries_only_its_own_subscriptions() -> None:
    gateway = EventGateway()
    first = gateway.subscriber(EventIdentity.host("first"))
    second = gateway.subscriber(EventIdentity.host("second"))
    first.subscribe("demo.one", lambda event: None)
    first.subscribe("demo.two", lambda event: None)
    second.subscribe("demo.one", lambda event: None)

    own = first.subscriptions()
    assert [(item.event, item.owner.subject) for item in own] == [
        ("demo.one", "host:first"),
        ("demo.two", "host:first"),
    ]
    assert len(gateway.subscriptions()) == 3
    assert first.subscriber_count("demo.one") == 1
    assert gateway.subscriber_count("demo.one") == 2
    await gateway.stop()


@pytest.mark.asyncio
async def test_gateway_routes_while_transport_only_moves_events() -> None:
    class RecordingTransport(EventTransport):
        def __init__(self) -> None:
            self.dispatch = None
            self.sent: list[str] = []

        def bind(self, dispatch):
            self.dispatch = dispatch

        async def send(self, event):
            self.sent.append(event.name)
            assert self.dispatch is not None
            await self.dispatch(event)

        async def flush(self):
            return None

        async def stop(self):
            return None

    transport = RecordingTransport()
    gateway = EventGateway(transport=transport)
    seen: list[str] = []
    gateway.subscribe("demo", lambda event: seen.append(event.name))

    await gateway.publish(Event("demo"))

    assert transport.sent == ["demo"]
    assert seen == ["demo"]
    assert gateway.subscriptions()[0].event == "demo"
    await gateway.stop()


@pytest.mark.asyncio
async def test_jsonl_sink_writes_safe_payload(tmp_path) -> None:
    log_path = tmp_path / "events.jsonl"
    bus = EventBus()
    bus.subscribe("*", jsonl_sink(log_path))

    await bus.publish(Event("demo", {"text": "你好", "obj": object()}))
    await bus.flush()

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "demo"
    assert record["payload"]["text"] == "你好"
    assert isinstance(record["payload"]["obj"], str)
    await bus.stop()


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
    await bus.flush()

    assert (recording.turn_starts, recording.llm_responses) == (1, 1)
    assert (recording.tool_afters, recording.turn_ends) == (1, 1)
    await bus.stop()


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
    await bus.flush()

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
    await bus.stop()


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
    await bus.flush()

    assert names == ["memory.write", "memory.update", "memory.delete"]
    await bus.stop()
