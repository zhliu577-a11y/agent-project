# tests/test_local_tools.py —— 本地工具插件：注册、执行与权限闸门（不联网）
from pathlib import Path

import pytest

from core.hooks import HookGateway, LifecycleHooks
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import ModelResponse, ToolCall
from loop import run_agent
from plugins.loader import load_tool_plugins

pytestmark = pytest.mark.asyncio


def _write_tool_plugin(tmp_path: Path, module: str) -> Path:
    plugin_dir = tmp_path / "tools" / "text"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        '{"name": "text", "type": "tool", '
        '"entry": {"module": "tool.py", "factory": "create_tools"}}',
        encoding="utf-8",
    )
    (plugin_dir / "tool.py").write_text(module, encoding="utf-8")
    return plugin_dir


_ECHO_MODULE = """
from core.tool import Tool


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        return "echo:" + kwargs["text"]


def create_tools(plugin_dir):
    return EchoTool()
"""


def _registered(tmp_path: Path) -> ToolRegistry:
    plugins = load_tool_plugins(tmp_path)
    registry = ToolRegistry()
    for _, tools in plugins:
        for tool in tools:
            registry.register(tool)
    return registry


async def test_local_tool_registers_and_executes_directly(tmp_path) -> None:
    _write_tool_plugin(tmp_path, _ECHO_MODULE)
    registry = _registered(tmp_path)

    assert registry.describe("text__echo") is not None
    result = await registry.execute("text__echo", {"text": "hi"})
    assert result == "echo:hi"


class FakeModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


class DenyAll(LifecycleHooks):
    async def tool_before(self, ctx, tool_call):
        return "deny"


async def test_local_tool_goes_through_the_same_permission_gate(tmp_path) -> None:
    _write_tool_plugin(tmp_path, _ECHO_MODULE)
    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="text__echo", arguments={"text": "hi"})],
            ),
            ModelResponse(content="已被拒绝", tool_calls=[]),
        ]
    )
    hooks = HookGateway()
    hooks.add(DenyAll())

    ctx = await run_agent(model, _registered(tmp_path), hooks, "你是助手", "回显 hi")

    assert any("拒绝" in message.content for message in ctx.messages)
    assert not any("echo:hi" in message.content for message in ctx.messages)
    assert ctx.stop_reason == "done"


async def test_repo_json_tool_plugin_is_usable() -> None:
    """仓库自带的 json 工具插件应能被加载、注册并真实执行。"""
    root = Path(__file__).resolve().parents[1] / "plugins"
    by_name = {manifest.name: tools for manifest, tools in load_tool_plugins(root)}
    assert "json" in by_name, "plugins/tools/json 应当存在"

    registry = ToolRegistry()
    for tool in by_name["json"]:
        registry.register(tool)

    formatted = await registry.execute("json__format", {"text": '{"a":1,"b":"中文"}'})
    assert '"a": 1' in formatted
    assert "中文" in formatted

    picked = await registry.execute("json__get", {"text": '{"a":{"b":[1,2]}}', "path": "a.b.1"})
    assert picked == "2"

    missing = await registry.execute("json__get", {"text": "{}", "path": "x.y"})
    assert "不存在" in missing
