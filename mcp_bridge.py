# mcp_bridge.py —— MCP 工具桥接：把 MCP 服务器的工具接入 ToolRegistry
import asyncio
import threading
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from core.registry import ToolRegistry
from core.tool import Tool


def _result_to_text(result) -> str:
    """提取 MCP 返回结果中的文本内容。"""
    return "\n".join(c.text for c in result.content if c.type == "text")


class McpTool(Tool):
    """把 MCP 服务器上的一个工具包装成内核 Tool。"""

    def __init__(self, session, loop, name, description, input_schema) -> None:
        self._session = session
        self._loop = loop
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

    def execute(self, **kwargs: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(self._call(kwargs), self._loop)
        return future.result()

    async def _call(self, arguments: dict[str, Any]) -> str:
        result = await self._session.call_tool(self._name, arguments)
        return _result_to_text(result)


class McpBridge:
    """后台线程跑事件循环；会话生命周期只在一个任务内进出，避免 anyio 任务错乱。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        self._ready = threading.Event()
        self._tools: list[McpTool] = []
        self._error: Exception | None = None
        self._session_task = None
        self._stop_event = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def connect_stdio(self, command: str, args: list[str]) -> list[McpTool]:
        """连接一个 stdio MCP 服务器，返回它暴露的工具列表（同步等待就绪）。"""
        self._ready.clear()
        self._session_task = asyncio.run_coroutine_threadsafe(
            self._run_session(command, args), self._loop
        )
        if not self._ready.wait(timeout=30):
            raise TimeoutError("连接 MCP 服务器超时")
        if self._error is not None:
            raise self._error
        return self._tools

    async def _run_session(self, command: str, args: list[str]) -> None:
        """会话生命周期：只在本任务内进入和退出上下文。"""
        self._stop_event = asyncio.Event()
        try:
            async with stdio_client(
                StdioServerParameters(command=command, args=args)
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self._tools = [
                        McpTool(session, self._loop, t.name, t.description or "", t.input_schema)
                        for t in tools.tools
                    ]
                    self._ready.set()
                    await self._stop_event.wait()
        except Exception as exc:
            self._error = exc
            self._ready.set()

    def close(self) -> None:
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._session_task is not None:
            try:
                self._session_task.result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


def register_mcp_server(
    registry: ToolRegistry,
    bridge: McpBridge,
    command: str,
    args: list[str],
) -> list[str]:
    """连接 MCP 服务器并把所有工具注册进注册表，返回工具名列表。"""
    tools = bridge.connect_stdio(command, args)
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]
