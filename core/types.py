# core/types.py —— 内核数据结构
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    content: str
    tool_calls: list[ToolCall]
    raw: Any = None


@dataclass
class TurnContext:
    messages: list[Message]
    turn: int = 0
    max_turns: int = 20
    stop_reason: str = "max_turns"
    state: dict[str, Any] = field(default_factory=dict)


def message_to_dict(message: Message) -> dict[str, Any]:
    """把内核 Message 序列化为 JSON 友好的字典（供 SessionStore 等使用）。"""
    return {
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in message.tool_calls
        ],
    }


def message_from_dict(raw: dict[str, Any]) -> Message:
    """把 message_to_dict 的产物还原成 Message。"""
    return Message(
        role=raw["role"],
        content=raw.get("content", ""),
        tool_call_id=raw.get("tool_call_id", ""),
        tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
            for tc in raw.get("tool_calls", [])
        ],
    )
