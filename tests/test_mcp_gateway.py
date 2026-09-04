# tests/test_mcp_gateway.py —— MCP 网关单元测试（不联网；用假会话替代子进程）
import asyncio
from pathlib import Path

import pytest

from core.registry import ToolRegistry
from gateways.mcp_gateway import (
    McpGateway,
    McpTool,
    PluginConnection,
    UsePlugin,
    result_to_text,
)
from plugins.loader import McpPluginSpec, PluginManifest


def _spec(name: str = "time") -> McpPluginSpec:
    manifest = PluginManifest(
        name=name,
        type="mcp",
        version="",
        description="",
        enabled=True,
        directory=Path("."),
        entry={"command": "python", "args": ["server.py"]},
    )
    return McpPluginSpec(manifest=manifest, transport="stdio", command="python", args=["server.py"])


class FakeContent:
    def __init__(self, type_, text=None) -> None:
        self.type = type_
        self.text = text


class FakeResult:
    def __init__(self, items) -> None:
        self.content = items


class FakeSession:
    """记录调用并把结果回显的假 MCP 会话。"""

    def __init__(self, label: str = "ok") -> None:
        self._label = label
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return FakeResult([FakeContent("text", f"{self._label}:{name}")])


class SlowSession:
    """模拟一个迟迟不返回的 MCP 会话，用于验证工具调用超时。"""

    async def call_tool(self, name, arguments):
        await asyncio.sleep(1)
        return FakeResult([FakeContent("text", "太慢了")])


class FakeGateway(McpGateway):
    """不启动子进程的网关：connect_plugin 直接返回假会话与假工具。"""

    def __init__(self, specs: list[McpPluginSpec], raw_tools: list[str] | None = None) -> None:
        super().__init__(specs, connect_timeout=1.0, call_timeout=1.0)
        self._raw_tools = raw_tools or ["get_current_time"]
        self.connect_count = 0

    async def connect_plugin(self, spec):
        self.connect_count += 1
        session = FakeSession(spec.manifest.name)
        raw = [(name, f"描述-{name}", {}) for name in self._raw_tools]
        return PluginConnection(session=session, cleanup=[]), raw


def test_result_to_text_filters_text_only() -> None:
    result = FakeResult([FakeContent("text", "12:00:00"), FakeContent("image", None)])
    assert result_to_text(result) == "12:00:00"


@pytest.mark.asyncio
async def test_mcp_tool_name_is_namespaced_and_call_times_out() -> None:
    tool = McpTool(SlowSession(), "time", "get_current_time", "时间", {}, timeout=0.05)
    assert tool.name == "time__get_current_time"
    with pytest.raises(TimeoutError):
        await tool.execute()


@pytest.mark.asyncio
async def test_mount_exposes_namespaced_tools_and_is_idempotent() -> None:
    gateway = FakeGateway([_spec("time")])

    tools = await gateway.mount("time")
    assert [t.name for t in tools] == ["time__get_current_time"]
    assert [t.plugin_name for t in tools] == ["time"]
    assert gateway.status()["time"] == "loaded"

    await gateway.mount("time")  # 幂等：不产生第二个连接
    assert gateway.connect_count == 1

    await gateway.close()
    assert gateway.mounted() == []
    assert gateway.status()["time"] == "idle"


@pytest.mark.asyncio
async def test_mount_unknown_plugin_raises() -> None:
    gateway = FakeGateway([_spec("time")])
    with pytest.raises(KeyError, match="未知"):
        await gateway.mount("missing")


@pytest.mark.asyncio
async def test_same_raw_tool_name_across_plugins_stays_distinct() -> None:
    gateway = FakeGateway([_spec("time"), _spec("math")], raw_tools=["probe"])
    registry = ToolRegistry()
    for name in ("time", "math"):
        for tool in await gateway.mount(name):
            registry.register(tool)

    assert registry.describe("time__probe") is not None
    assert registry.describe("math__probe") is not None
    assert [t["function"]["name"] for t in registry.list_schemas()] == [
        "time__probe",
        "math__probe",
    ]


@pytest.mark.asyncio
async def test_use_plugin_mounts_registers_and_reports() -> None:
    gateway = FakeGateway([_spec("time")])
    registry = ToolRegistry()
    loader_tool = UsePlugin(gateway, registry)

    out = await loader_tool.execute(name="time")
    assert "已挂载" in out
    assert "time__get_current_time" in out
    assert registry.describe("time__get_current_time") is not None

    again = await loader_tool.execute(name="time")
    assert "已挂载" in again
    assert gateway.connect_count == 1  # 重复调用不重复连接

    unknown = await loader_tool.execute(name="missing")
    assert "未知插件" in unknown
    assert "可挂载" in unknown


@pytest.mark.asyncio
async def test_mcp_tool_routes_call_with_raw_tool_name() -> None:
    session = FakeSession("time")
    tool = McpTool(session, "time", "get_current_time", "时间", {}, timeout=1.0)
    result = await tool.execute(timezone="UTC")
    assert result == "time:get_current_time"
    assert session.calls == [("get_current_time", {"timezone": "UTC"})]
