# mcp_bridge.py —— MCP 工具桥接（异步版，支持多个服务器）
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core.registry import ToolRegistry
from core.tool import Tool


def _result_to_text(result) -> str:
    """提取 MCP 返回结果中的文本内容。"""
    return "\n".join(c.text for c in result.content if c.type == "text")


class McpTool(Tool):
    """把 MCP 服务器上的一个工具包装成内核 Tool（异步执行）。"""

    def __init__(self, session, name, description, input_schema) -> None:
        self._session = session
        self._name = name
        self._description = description
        self._schema = input_schema or {"type": "object", "properties": {}}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._schema

    async def execute(self, **kwargs: Any) -> Any:
        result = await self._session.call_tool(self._name, kwargs)
        return _result_to_text(result)


class McpBridge:
    """管理多个 MCP 服务器会话（异步）。"""

    def __init__(self) -> None:
        # 每个连接保存 (session_cm, stdio_cm)，close 时逐个清理
        self._connections: list[tuple[Any, Any]] = []

    async def connect_stdio(self, command: str, args: list[str]) -> list[McpTool]:
        stdio_cm = stdio_client(StdioServerParameters(command=command, args=args))
        read, write = await stdio_cm.__aenter__()
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        await session.initialize()

        # 记住连接，而不是覆盖：这样能同时连多个服务器
        self._connections.append((session_cm, stdio_cm))

        tools = await session.list_tools()
        return [McpTool(session, t.name, t.description or "", t.input_schema) for t in tools.tools]

    async def close(self) -> None:
        for session_cm, stdio_cm in reversed(self._connections):
            for cm in (session_cm, stdio_cm):
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        self._connections.clear()


async def register_mcp_server(
    registry: ToolRegistry,
    bridge: McpBridge,
    command: str,
    args: list[str],
) -> list[str]:
    """连接一个 MCP 服务器并把所有工具注册进注册表，返回工具名列表。"""
    tools = await bridge.connect_stdio(command, args)
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]
