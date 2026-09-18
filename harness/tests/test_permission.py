# tests/test_permission.py —— permission 钩子插件测试（不联网）
from pathlib import Path

import pytest

from core.hooks import HookGateway, LifecycleHooks
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.types import ModelResponse, ToolCall
from loop import run_agent
from plugins.hooks.permission.hook import PermissionHooks, Rule
from plugins.loader import load_hook_plugins

pytestmark = pytest.mark.asyncio


async def test_deny_rule_blocks_tool() -> None:
    hooks = HookGateway()
    hooks.add(PermissionHooks([Rule("filesystem__delete_file", "deny")]))
    allowed = await hooks.tool_before(
        None, ToolCall(id="1", name="filesystem__delete_file", arguments={})
    )
    assert allowed is False


async def test_unmatched_tool_allowed_by_default() -> None:
    hooks = HookGateway()
    hooks.add(PermissionHooks([Rule("filesystem__delete_file", "deny")]))
    allowed = await hooks.tool_before(None, ToolCall(id="1", name="math__calculate", arguments={}))
    assert allowed is True


async def test_repo_permission_plugin_is_loaded_via_loader() -> None:
    """仓库自带的 permission 插件应能通过插件目录加载并拦截危险工具。"""
    root = Path(__file__).resolve().parents[1] / "plugins"
    loaded = [(m, hook) for m, hook in load_hook_plugins(root) if m.name == "permission"]
    assert loaded, "plugins/hooks/permission 应当被加载"
    # loader 动态导入会产生独立的类对象，因此用类名而非 isinstance 判断
    policy = next(hook for _, hook in loaded if type(hook).__name__ == "PermissionHooks")
    allowed = await policy.tool_before(
        None, ToolCall(id="1", name="filesystem__delete_file", arguments={})
    )
    assert allowed == "deny"


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
    async def tool_before(self, ctx, tool_call):
        return "deny"


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
    hooks = HookGateway()
    hooks.add(DenyAll())

    ctx = await run_agent(model, tools, hooks, "你是助手", "现在几点")

    assert counter["n"] == 0  # 工具从未执行
    assert any("拒绝" in m.content for m in ctx.messages)  # 模型看到拒绝原因
    assert ctx.stop_reason == "done"
