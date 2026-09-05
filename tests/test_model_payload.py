# tests/test_model_payload.py —— 消息序列化测试（不需要联网）
from core.types import Message, ToolCall
from plugins.model.deepseek.model import message_to_payload


def test_assistant_without_tool_calls() -> None:
    payload = message_to_payload(Message(role="assistant", content="你好"))
    assert payload["role"] == "assistant"
    assert "tool_calls" not in payload


def test_assistant_with_tool_calls() -> None:
    msg = Message(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id="1", name="get_time", arguments={"city": "北京"})],
    )
    payload = message_to_payload(msg)
    assert payload["tool_calls"][0]["function"]["name"] == "get_time"
    assert payload["tool_calls"][0]["function"]["arguments"] == '{"city": "北京"}'


def test_tool_message_has_call_id() -> None:
    payload = message_to_payload(Message(role="tool", content="晴", tool_call_id="1"))
    assert payload["role"] == "tool"
    assert payload["tool_call_id"] == "1"
