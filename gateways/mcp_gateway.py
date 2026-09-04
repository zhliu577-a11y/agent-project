# gateways/mcp_gateway.py —— MCP 网关：内核与所有 MCP 插件之间的唯一通道
#
# 拓扑：
#   agent loop ──(唯一入口)──> McpGateway ──> 各 MCP 插件服务器（stdio）
#
# 只有 McpGateway 会与 MCP 服务器建立/维护/清理连接；工具以
#   <插件名>__<工具名>
# 的命名空间形式暴露，避免多插件同名工具互相覆盖。模型通过 use_plugin
# 按需挂载插件，网关负责把新工具同步进工具注册表。
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core.registry import ToolRegistry
from core.tool import Tool
from plugins.loader import McpPluginSpec

logger = logging.getLogger(__name__)


def result_to_text(result) -> str:
    """提取 MCP 返回结果中的文本内容。"""
    return "\n".join(c.text for c in result.content if c.type == "text")


async def _close_cm(cm) -> None:
    """安全关闭一个异步上下文管理器（失败静默）。"""
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


@dataclass
class PluginConnection:
    """一个已建立的 MCP 插件连接：会话 + 需要清理的上下文管理器。"""

    session: Any
    cleanup: list[Any]


class McpTool(Tool):
    """把 MCP 服务器上的一个工具包装成内核 Tool（命名空间 + 超时）。"""

    def __init__(
        self,
        session,
        plugin_name: str,
        tool_name: str,
        description: str,
        input_schema,
        timeout: float = 30.0,
    ) -> None:
        self._session = session
        self._plugin_name = plugin_name
        self._tool_name = tool_name
        self._description = description
        self._schema = input_schema or {"type": "object", "properties": {}}
        self._timeout = timeout

    @property
    def name(self) -> str:
        return f"{self._plugin_name}__{self._tool_name}"

    @property
    def plugin_name(self) -> str:
        return self._plugin_name

    @property
    def tool_name(self) -> str:
        return self._tool_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> Any:
        async with asyncio.timeout(self._timeout):
            result = await self._session.call_tool(self._tool_name, kwargs)
        return result_to_text(result)


class McpGateway:
    """MCP 网关：内核与 MCP 世界之间的唯一通道。

    职责边界：
    - 持有全部 MCP 插件规格（由 plugins/loader.py 扫描 plugins/mcp/ 得到）；
    - 只有本类会连接 MCP 服务器、维护会话并在退出时清理；
    - 为每个工具生成 <插件名>__<工具名> 的稳定命名；
    - mount 幂等：重复挂载不产生第二个连接。

    目前网关是进程内的聚合器。未来若要把网关换成独立进程/远程代理，
    只需提供一个实现相同 mount/close 边界的替代品，内核无需感知。
    """

    def __init__(
        self,
        plugins: list[McpPluginSpec],
        connect_timeout: float | None = None,
        call_timeout: float | None = None,
    ) -> None:
        self._plugins = {spec.manifest.name: spec for spec in plugins}
        self._connect_timeout = (
            float(connect_timeout)
            if connect_timeout is not None
            else float(os.getenv("MCP_CONNECT_TIMEOUT", "20"))
        )
        self._call_timeout = (
            float(call_timeout)
            if call_timeout is not None
            else float(os.getenv("MCP_CALL_TIMEOUT", "30"))
        )
        self._connections: dict[str, PluginConnection] = {}
        self._tools: dict[str, McpTool] = {}
        self._failed: set[str] = set()

    # ---- 目录/状态（只读，供 use_plugin 与日志使用）----

    def available(self) -> list[str]:
        return sorted(self._plugins)

    def mounted(self) -> list[str]:
        return sorted(self._connections)

    def plugin_spec(self, name: str) -> McpPluginSpec | None:
        return self._plugins.get(name)

    def tool_names(self, plugin_name: str) -> list[str]:
        return [tool.name for tool in self._tools.values() if tool.plugin_name == plugin_name]

    def status(self) -> dict[str, str]:
        states: dict[str, str] = {}
        for name in self._plugins:
            if name in self._connections:
                states[name] = "loaded"
            elif name in self._failed:
                states[name] = "failed"
            else:
                states[name] = "idle"
        return states

    # ---- 连接与清理 ----

    async def connect_plugin(
        self, spec: McpPluginSpec
    ) -> tuple[PluginConnection, list[tuple[str, str, dict[str, Any]]]]:
        """连接一个 stdio MCP 插件并列出其原始工具；失败时清理半开连接。

        返回 (连接记录, [(原始工具名, 描述, 参数 schema), ...])。
        这是子类替换的测试缝：测试可用假会话替代真实子进程。
        """
        stdio_cm = stdio_client(
            StdioServerParameters(
                command=spec.command,
                args=spec.args,
                cwd=str(spec.manifest.directory),
            )
        )
        session_cm: Any = None
        cleanup: list[Any] = []
        try:
            async with asyncio.timeout(self._connect_timeout):
                read, write = await stdio_cm.__aenter__()
                cleanup.append(stdio_cm)
                session_cm = ClientSession(read, write)
                session = await session_cm.__aenter__()
                cleanup.append(session_cm)
                await session.initialize()
                listed = await session.list_tools()
        except TimeoutError as exc:
            await self._close_cleanup(cleanup)
            raise TimeoutError(
                f"连接 MCP 插件 '{spec.manifest.name}' 超时"
                f"（{self._connect_timeout:g} 秒）: {spec.command}"
            ) from exc
        except Exception:
            await self._close_cleanup(cleanup)
            raise

        raw_tools = [
            (tool.name, tool.description or "", tool.input_schema or {}) for tool in listed.tools
        ]
        return PluginConnection(session=session, cleanup=cleanup), raw_tools

    @staticmethod
    async def _close_cleanup(cleanup: list[Any]) -> None:
        for cm in reversed(cleanup):
            await _close_cm(cm)

    async def mount(self, name: str) -> list[McpTool]:
        """挂载一个 MCP 插件并返回其命名空间工具；幂等：已挂载则直接返回。"""
        spec = self._plugins.get(name)
        if spec is None:
            raise KeyError(f"未知的 MCP 插件: {name}，可用: {list(self._plugins)}")
        if name in self._connections:
            return [tool for tool in self._tools.values() if tool.plugin_name == name]

        try:
            connection, raw_tools = await self.connect_plugin(spec)
        except Exception as exc:
            self._failed.add(name)
            logger.exception("MCP 插件 %s 挂载失败", name)
            raise RuntimeError(f"插件 {name} 连接失败: {exc}") from exc

        tools = [
            McpTool(
                connection.session,
                plugin_name=name,
                tool_name=raw_name,
                description=description,
                input_schema=schema,
                timeout=self._call_timeout,
            )
            for raw_name, description, schema in raw_tools
        ]
        self._connections[name] = connection
        self._failed.discard(name)
        for tool in tools:
            self._tools[tool.name] = tool
        logger.info(
            "MCP 插件 %s 已挂载（%d 个工具）: %s",
            name,
            len(tools),
            [tool.name for tool in tools],
        )
        return tools

    async def close(self) -> None:
        """关闭全部插件连接并清空工具表（幂等，可重复调用）。"""
        for connection in reversed(list(self._connections.values())):
            await self._close_cleanup(connection.cleanup)
        self._connections.clear()
        self._tools.clear()
        self._failed.clear()


class UsePlugin(Tool):
    """把"按需挂载 MCP 插件"本身做成一个工具：模型决定后，网关负责连接。"""

    name = "use_plugin"
    description = (
        "按需挂载一个 MCP 插件并注册其全部工具；"
        "挂载成功后工具以 <插件名>__<工具名> 的形式出现在工具池里。"
    )

    def __init__(self, gateway: McpGateway, registry: ToolRegistry) -> None:
        self._gateway = gateway
        self._registry = registry
        self._mounted: set[str] = set()

    @property
    def parameters(self) -> dict[str, Any]:
        available = gateway_available(self._gateway)
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"要挂载的 MCP 插件名，可选：{available}",
                }
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        if self._gateway.plugin_spec(name) is None:
            return f"未知插件: {name}，可挂载: {gateway_available(self._gateway)}"
        if name in self._mounted:
            return f"插件 {name} 已挂载，工具: {self._gateway.tool_names(name)}"

        try:
            tools = await self._gateway.mount(name)
        except Exception as exc:
            logger.exception("插件 %s 挂载失败", name)
            return f"插件 {name} 挂载失败: {exc}"

        for tool in tools:
            self._registry.register(tool)
        self._mounted.add(name)
        return f"插件 {name} 已挂载，可用工具: {[tool.name for tool in tools]}"


def gateway_available(gateway: McpGateway) -> str:
    names = gateway.available()
    return "、".join(names) if names else "（无可用插件）"
