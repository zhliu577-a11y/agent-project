# mcp_bridge.py —— MCP 工具桥接（异步版，支持多服务器、超时与连接清理）
import asyncio
import os
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core.registry import ToolRegistry
from core.tool import Tool


def _result_to_text(result) -> str:
    """提取 MCP 返回结果中的文本内容。"""
    return "\n".join(c.text for c in result.content if c.type == "text")


async def _close_cm(cm) -> None:
    """安全关闭一个异步上下文管理器（失败静默）。"""
    if cm is not None:
        try:
            await cm.__aexit__(None, None, None)
        except Exception:
            pass


class McpTool(Tool):
    """把 MCP 服务器上的一个工具包装成内核 Tool（异步执行，带超时）。"""

    def __init__(
        self,
        session,
        name: str,
        description: str,
        input_schema,
        timeout: float = 30.0,
    ) -> None:
        self._session = session
        self._name = name
        self._description = description
        self._schema = input_schema or {"type": "object", "properties": {}}
        self._timeout = timeout

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
        async with asyncio.timeout(self._timeout):
            result = await self._session.call_tool(self._name, kwargs)
        return _result_to_text(result)


class McpBridge:
    """管理多个 MCP 服务器会话（异步，连接带超时）。"""

    def __init__(self) -> None:
        # 每个连接保存 (session_cm, stdio_cm)，close 时逐个清理
        self._connections: list[tuple[Any, Any]] = []

    async def connect_stdio(
        self,
        command: str,
        args: list[str],
        timeout: float | None = None,
    ) -> list[McpTool]:
        """连接一个 stdio MCP 服务器；超时或失败时自动清理已建立的连接。"""
        timeout = timeout if timeout is not None else float(os.getenv("MCP_CONNECT_TIMEOUT", "20"))
        call_timeout = float(os.getenv("MCP_CALL_TIMEOUT", "30"))

        stdio_cm = stdio_client(StdioServerParameters(command=command, args=args))
        session_cm = None
        session = None
        try:
            async with asyncio.timeout(timeout):
                read, write = await stdio_cm.__aenter__()
                session_cm = ClientSession(read, write)
                session = await session_cm.__aenter__()
                await session.initialize()
                tools = await session.list_tools()
        except TimeoutError as exc:
            await _close_cm(session_cm)
            await _close_cm(stdio_cm)
            raise TimeoutError(f"连接 MCP 服务器超时（{timeout:g} 秒）: {command}") from exc
        except Exception:
            await _close_cm(session_cm)
            await _close_cm(stdio_cm)
            raise

        self._connections.append((session_cm, stdio_cm))
        return [
            McpTool(
                session,
                t.name,
                t.description or "",
                t.input_schema,
                timeout=call_timeout,
            )
            for t in tools.tools
        ]

    async def close(self) -> None:
        for session_cm, stdio_cm in reversed(self._connections):
            await _close_cm(session_cm)
            await _close_cm(stdio_cm)
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
