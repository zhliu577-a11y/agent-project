# core/types.py —— 内核数据结构
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str                    # "system" | "user" | "assistant" | "tool"
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
