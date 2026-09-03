# tests/test_mcp_bridge.py —— MCP 桥接的纯函数测试（不需要联网）
from mcp_bridge import _result_to_text


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
