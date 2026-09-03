# mcp_bridge.py —— MCP 工具桥接（异步版）
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
    """管理 MCP 会话生命周期（异步，与 asyncio.run(main()) 配合使用）。"""

    def __init__(self) -> None:
        self._stdio_cm = None
        self._session_cm = None
        self._session = None

    async def connect_stdio(self, command: str, args: list[str]) -> list[McpTool]:
        self._stdio_cm = stdio_client(
            StdioServerParameters(command=command, args=args)
        )
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()

        tools = await self._session.list_tools()
        return [
            McpTool(self._session, t.name, t.description or "", t.input_schema)
            for t in tools.tools
        ]

    async def close(self) -> None:
        for cm in (self._session_cm, self._stdio_cm):
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
        self._session_cm = None
        self._stdio_cm = None
        self._session = None


async def register_mcp_server(
    registry: ToolRegistry,
    bridge: McpBridge,
    command: str,
    args: list[str],
) -> list[str]:
    """连接 MCP 服务器并把所有工具注册进注册表，返回工具名列表。"""
    tools = await bridge.connect_stdio(command, args)
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]