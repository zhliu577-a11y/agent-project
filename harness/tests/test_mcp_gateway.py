# tests/test_mcp_gateway.py —— MCP 网关单元测试（不联网；用假会话替代子进程）
import asyncio
from pathlib import Path

import pytest

from core.errors import DeclaredPluginError, ToolError
from core.registry import ToolRegistry
from core.retry import RetryDecision, RetryExecutor, RetryRequest
from gateways.mcp_gateway import (
    McpGateway,
    McpTool,
    PluginConnection,
    UsePlugin,
    _tool_retry_safe,
    result_to_text,
)
from plugins.loader import DeclaredError, McpPluginSpec, PluginManifest


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
    def __init__(self, items, is_error: bool = False) -> None:
        self.content = items
        self.isError = is_error
        self.is_error = is_error  # 兼容 SDK 的 snake_case 字段


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


class ErrorSession:
    """返回 isError 结果的假会话，用于验证错误契约匹配。"""

    def __init__(self, text: str) -> None:
        self._text = text

    async def call_tool(self, name, arguments):
        return FakeResult([FakeContent("text", self._text)], is_error=True)


_RATE_LIMIT = DeclaredError(
    code="rate_limited", category="retryable", hint="稍后重试", retryable=True
)


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
async def test_use_plugin_is_safe_after_runtime_preload() -> None:
    gateway = FakeGateway([_spec("time")])
    registry = ToolRegistry()
    loader_tool = UsePlugin(gateway, registry)

    ok, message = await loader_tool.mount("time")
    assert ok is True
    assert "已挂载" in message
    assert registry.describe("time__get_current_time") is not None

    again = await loader_tool.execute(name="time")
    assert "已挂载" in again
    assert gateway.connect_count == 1
    assert [schema["function"]["name"] for schema in registry.list_schemas()] == [
        "time__get_current_time"
    ]


@pytest.mark.asyncio
async def test_mcp_tool_routes_call_with_raw_tool_name() -> None:
    session = FakeSession("time")
    tool = McpTool(session, "time", "get_current_time", "时间", {}, timeout=1.0)
    result = await tool.execute(timezone="UTC")
    assert result == "time:get_current_time"
    assert session.calls == [("get_current_time", {"timezone": "UTC"})]


@pytest.mark.asyncio
async def test_mcp_is_error_matches_declared_code() -> None:
    tool = McpTool(
        ErrorSession("[rate_limited] 上游限流"),
        "upstream",
        "query",
        "查询",
        {},
        timeout=1.0,
        declarations={"rate_limited": _RATE_LIMIT},
    )
    with pytest.raises(DeclaredPluginError) as info:
        await tool.execute()
    assert info.value.code == "rate_limited"
    assert info.value.category == "retryable"
    assert info.value.hint == "稍后重试"


@pytest.mark.asyncio
async def test_mcp_is_error_without_declaration_falls_back_to_tool_error() -> None:
    tool = McpTool(
        ErrorSession("something exploded"),
        "upstream",
        "query",
        "查询",
        {},
        timeout=1.0,
        declarations={"rate_limited": _RATE_LIMIT},
    )
    with pytest.raises(ToolError, match="something exploded"):
        await tool.execute()


@pytest.mark.asyncio
async def test_mcp_is_error_matches_code_wrapped_by_sdk_message() -> None:
    """SDK 会把工具异常包装成 'Error executing tool x: ...'，code 需任意位置匹配。"""
    tool = McpTool(
        ErrorSession("Error executing tool query: [rate_limited] 上游限流"),
        "upstream",
        "query",
        "查询",
        {},
        timeout=1.0,
        declarations={"rate_limited": _RATE_LIMIT},
    )
    with pytest.raises(DeclaredPluginError) as info:
        await tool.execute()
    assert info.value.code == "rate_limited"


class FlakySession:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("transient")
        return FakeResult([FakeContent("text", "ok")])


class FlakyErrorSession:
    def __init__(self) -> None:
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.calls == 1:
            return FakeResult(
                [FakeContent("text", "[rate_limited] upstream busy")],
                is_error=True,
            )
        return FakeResult([FakeContent("text", "ok")])


@pytest.mark.asyncio
async def test_mcp_tool_uses_retry_and_recovery_callback() -> None:
    class AllowRetry:
        async def decide(self, request: RetryRequest) -> RetryDecision:
            return RetryDecision(retry=True, delay=0, reason="test")

    executor = RetryExecutor(
        {"allow": AllowRetry()},
        default_policy="allow",
        max_attempts=2,
        max_delay=0,
        total_timeout=1,
    )
    recovered = 0

    async def recover(request: RetryRequest) -> None:
        nonlocal recovered
        recovered += 1

    session = FlakySession()
    tool = McpTool(
        session,
        "time",
        "get_current_time",
        "时间",
        {},
        timeout=1,
        retry=executor,
        retry_safe=True,
        recover=recover,
    )

    assert await tool.execute(timezone="UTC") == "ok"
    assert session.calls == 2
    assert recovered == 1


@pytest.mark.asyncio
async def test_mcp_declared_retryable_error_uses_retry_policy() -> None:
    class AllowRetry:
        async def decide(self, request: RetryRequest) -> RetryDecision:
            return RetryDecision(retry=True, delay=0, reason="test")

    executor = RetryExecutor(
        {"allow": AllowRetry()},
        default_policy="allow",
        max_attempts=2,
        max_delay=0,
        total_timeout=1,
    )
    session = FlakyErrorSession()
    tool = McpTool(
        session,
        "upstream",
        "query",
        "query",
        {},
        timeout=1,
        declarations={"rate_limited": _RATE_LIMIT},
        retry=executor,
        retry_safe=True,
    )

    assert await tool.execute() == "ok"
    assert session.calls == 2


def test_mcp_retry_safe_uses_server_annotations() -> None:
    spec = _spec("time")
    assert _tool_retry_safe(spec, None) is False
    assert _tool_retry_safe(spec, {"readOnlyHint": True}) is True
    assert _tool_retry_safe(spec, {"idempotentHint": True}) is True


@pytest.mark.asyncio
async def test_mcp_gateway_reconnects_before_retrying_read_only_tool() -> None:
    class AllowRetry:
        async def decide(self, request: RetryRequest) -> RetryDecision:
            return RetryDecision(retry=True, delay=0, reason="test")

    class RecoveringGateway(McpGateway):
        def __init__(self, spec: McpPluginSpec) -> None:
            super().__init__(
                [spec],
                retry=RetryExecutor(
                    {"allow": AllowRetry()},
                    default_policy="allow",
                    max_attempts=2,
                    max_delay=0,
                    total_timeout=1,
                ),
            )
            self.connect_count = 0

        async def connect_plugin(self, spec):
            self.connect_count += 1
            session = FlakySession() if self.connect_count == 1 else FakeSession("ok")
            return (
                PluginConnection(session=session, cleanup=[]),
                [("query", "query", {}, {"readOnlyHint": True})],
            )

    gateway = RecoveringGateway(_spec("time"))
    tool = (await gateway.mount("time"))[0]

    assert await tool.execute() == "ok:query"
    assert gateway.connect_count == 2
