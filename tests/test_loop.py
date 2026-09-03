# tests/test_loop.py —— loop 的单元测试（异步）
import pytest

from core.hooks import HookManager
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.types import Message, ModelResponse, ToolCall
from loop import run_agent

pytestmark = pytest.mark.asyncio


class FakeModel(ModelAdapter):
    """测试用的假模型：按预设剧本依次返回回复。"""

    def __init__(self, script: list[ModelResponse]) -> None:
        self._script = list(script)

    async def complete(self, messages, tool_schemas) -> ModelResponse:
        return self._script.pop(0)


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
