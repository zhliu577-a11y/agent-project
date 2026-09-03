# tests/test_mcp_bridge.py —— MCP 桥接测试（不需要联网）
import asyncio

import pytest

from mcp_bridge import McpTool, _result_to_text

pytestmark = pytest.mark.asyncio


class FakeContent:
    def __init__(self, type_, text=None) -> None:
        self.type = type_
        self.text = text


class FakeResult:
    def __init__(self, items) -> None:
        self.content = items


def test_result_to_text_filters_text_only() -> None:
    result = FakeResult([FakeContent("text", "12:00:00"), FakeContent("image", None)])
    assert _result_to_text(result) == "12:00:00"


class SlowSession:
    """模拟一个迟迟不返回的 MCP 会话，用于验证工具调用超时。"""

    async def call_tool(self, name, arguments):
        await asyncio.sleep(1)
        return FakeResult([FakeContent("text", "太慢了")])


async def test_mcp_tool_call_times_out() -> None:
    tool = McpTool(SlowSession(), "slow", "慢工具", {}, timeout=0.05)
    with pytest.raises(TimeoutError):
        await tool.execute()
