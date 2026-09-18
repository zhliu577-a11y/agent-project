# tests/test_model_payload.py —— 消息序列化测试（不需要联网）
from core.types import Message, ToolCall
from plugins.model.deepseek.model import message_to_payload
from plugins.model.sub2api.model import (
    messages_to_responses_input,
    tool_schemas_to_responses,
)


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


def test_responses_input_separates_instructions_and_tool_results() -> None:
    instructions, items = messages_to_responses_input(
        [
            Message(role="system", content="system rules"),
            Message(role="user", content="hello"),
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hi"})],
            ),
            Message(role="tool", content="hi", tool_call_id="call_1"),
        ]
    )

    assert instructions == "system rules"
    assert items[0] == {"role": "user", "content": "hello"}
    assert items[1]["type"] == "function_call"
    assert items[1]["call_id"] == "call_1"
    assert items[2] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "hi",
    }


def test_responses_tool_schema_is_flattened() -> None:
    tools = tool_schemas_to_responses(
        [
            {
                "type": "function",
                "function": {
                    "name": "text__slugify",
                    "description": "Slugify text",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                },
            }
        ]
    )

    assert tools == [
        {
            "type": "function",
            "name": "text__slugify",
            "description": "Slugify text",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
            },
        }
    ]
