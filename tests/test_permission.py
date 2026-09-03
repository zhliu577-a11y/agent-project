# tests/test_permission.py —— 权限策略测试
import pytest

from core.hooks import HookManager, LifecycleHooks
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.types import ModelResponse, ToolCall
from loop import run_agent
from permission import PermissionHooks, Rule

pytestmark = pytest.mark.asyncio


async def test_deny_rule_blocks_tool() -> None:
    hooks = HookManager()
    hooks.add(PermissionHooks([Rule("delete_file", "deny")]))
    allowed = await hooks.tool_before(None, ToolCall(id="1", name="delete_file", arguments={}))
    assert allowed is False


async def test_unmatched_tool_allowed_by_default() -> None:
    hooks = HookManager()
    hooks.add(PermissionHooks([Rule("delete_file", "deny")]))
    allowed = await hooks.tool_before(None, ToolCall(id="1", name="get_time", arguments={}))
    assert allowed is True


class FakeModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


class CountingTime(Tool):
    name = "get_time"
    description = "返回当前时间"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, counter: dict) -> None:
        self._counter = counter

    async def execute(self, **kwargs):
        self._counter["n"] += 1
        return "12:00:00"


class DenyAll(LifecycleHooks):
    async def tool_before(self, ctx, tool_call) -> bool:
        return False


async def test_loop_does_not_execute_denied_tool() -> None:
    counter = {"n": 0}
    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="get_time", arguments={})],
            ),
            ModelResponse(content="已拒绝", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(CountingTime(counter))
    hooks = HookManager()
    hooks.add(DenyAll())

    ctx = await run_agent(model, tools, hooks, "你是助手", "现在几点")

    assert counter["n"] == 0  # 工具从未执行
    assert any("拒绝" in m.content for m in ctx.messages)  # 模型看到拒绝原因
    assert ctx.stop_reason == "done"
