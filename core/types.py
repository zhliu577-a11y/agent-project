# core/types.py —— 内核数据结构
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str = ""
    id: str = field(default_factory=lambda: uuid4().hex)
    timestamp: str = field(default_factory=_now)
    turn_id: str = ""
    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


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
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "timestamp": message.timestamp,
        "turn_id": message.turn_id,
        "run_id": message.run_id,
        "metadata": dict(message.metadata),
        "tool_calls": [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments} for tc in message.tool_calls
        ],
    }


def message_from_dict(raw: dict[str, Any]) -> Message:
    """把 message_to_dict 的产物还原成 Message。"""
    return Message(
        id=str(raw.get("id") or uuid4().hex),
        role=raw["role"],
        content=raw.get("content", ""),
        tool_call_id=raw.get("tool_call_id", ""),
        timestamp=str(raw.get("timestamp") or _now()),
        turn_id=str(raw.get("turn_id", "")),
        run_id=str(raw.get("run_id", "")),
        metadata=dict(raw.get("metadata", {})),
        tool_calls=[
            ToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("arguments", {}))
            for tc in raw.get("tool_calls", [])
        ],
    )
