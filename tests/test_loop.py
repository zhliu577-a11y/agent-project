# tests/test_loop.py —— loop 的单元测试（异步）
import asyncio

import pytest

from core.hooks import HookManager
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.types import ModelResponse, ToolCall
from loop import run_agent

pytestmark = pytest.mark.asyncio


class FakeModel(ModelAdapter):
    """测试用的假模型：按预设剧本依次返回回复。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        resp = self._script.pop(0)
        if on_token is not None and not resp.tool_calls:
            on_token(resp.content)
        return resp


class GetTime(Tool):
    name = "get_time"
    description = "返回当前时间"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "2026-09-01 12:00:00"


async def test_loop_ends_when_model_does_not_call_tools() -> None:
    model = FakeModel([ModelResponse(content="你好", tool_calls=[])])
    ctx = await run_agent(model, ToolRegistry(), HookManager(), "你是助手", "你好")
    assert ctx.stop_reason == "done"
    assert ctx.messages[-1].content == "你好"


async def test_loop_executes_tool_then_returns_final_answer() -> None:
    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="get_time", arguments={})],
            ),
            ModelResponse(content="现在是12点", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(GetTime())
    ctx = await run_agent(model, tools, HookManager(), "你是助手", "现在几点")
    assert ctx.stop_reason == "done"
    assert any(m.role == "tool" for m in ctx.messages)
    assert ctx.messages[-1].content == "现在是12点"


async def test_loop_stops_at_max_turns() -> None:
    script = [
        ModelResponse(
            content="",
            tool_calls=[ToolCall(id=str(i), name="get_time", arguments={})],
        )
        for i in range(100)
    ]
    model = FakeModel(script)
    tools = ToolRegistry()
    tools.register(GetTime())
    ctx = await run_agent(model, tools, HookManager(), "你是助手", "一直调用", max_turns=3)
    assert ctx.stop_reason == "max_turns"
    assert ctx.turn == 3


async def test_loop_streams_final_answer_tokens() -> None:
    model = FakeModel([ModelResponse(content="你好世界", tool_calls=[])])
    received: list[str] = []
    ctx = await run_agent(
        model,
        ToolRegistry(),
        HookManager(),
        "你是助手",
        "你好",
        on_token=received.append,
    )
    assert received == ["你好世界"]
    assert ctx.stop_reason == "done"


class PauseTool(Tool):
    """带停顿的工具：用于验证多个工具是否真的并行执行。"""

    def __init__(self, label: str) -> None:
        self._label = label

    @property
    def name(self) -> str:
        return f"pause_{self._label}"

    description = "停顿 0.1 秒后返回"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        order.append(f"start-{self._label}")
        await asyncio.sleep(0.1)
        order.append(f"end-{self._label}")
        return f"done-{self._label}"


order: list[str] = []


async def test_loop_executes_multiple_tools_in_parallel() -> None:
    order.clear()
    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCall(id="1", name="pause_a", arguments={}),
                    ToolCall(id="2", name="pause_b", arguments={}),
                ],
            ),
            ModelResponse(content="完成", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(PauseTool("a"))
    tools.register(PauseTool("b"))

    ctx = await run_agent(model, tools, HookManager(), "你是助手", "并行执行")

    starts = [i for i, s in enumerate(order) if s.startswith("start")]
    ends = [i for i, s in enumerate(order) if s.startswith("end")]
    # 并行时第二个工具会在第一个结束前启动；串行时不可能
    assert starts[1] < ends[0]
    assert ctx.stop_reason == "done"


class AlwaysFail(Tool):
    """每次执行都抛错的工具：用于验证失败计数与自动禁用。"""

    name = "fail_tool"
    description = "总是失败的工具"
    parameters = {"type": "object", "properties": {"x": {"type": "string"}}}

    def __init__(self, counter: dict) -> None:
        self._counter = counter

    async def execute(self, **kwargs):
        self._counter["n"] += 1
        raise ValueError("参数 x 格式不正确")


async def test_loop_disables_tool_after_consecutive_failures() -> None:
    counter = {"n": 0}
    tool_call = lambda i: ModelResponse(  # noqa: E731
        content="",
        tool_calls=[ToolCall(id=str(i), name="fail_tool", arguments={"x": "bad"})],
    )
    script = [tool_call(1), tool_call(2), tool_call(3), tool_call(4)]
    script.append(ModelResponse(content="我换一种方法", tool_calls=[]))

    model = FakeModel(script)
    tools = ToolRegistry()
    tools.register(AlwaysFail(counter))

    ctx = await run_agent(model, tools, HookManager(), "你是助手", "试试失败工具")

    assert counter["n"] == 3  # 第 4 次调用被禁用，未真正执行
    assert any("已连续失败" in m.content for m in ctx.messages)
    assert "fail_tool" in ctx.state["blocked_tools"]
    assert ctx.stop_reason == "done"
