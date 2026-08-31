# core/types.py —— 内核数据结构
from dataclasses import dataclass, field
from typing import Any

@dataclass
class Message:
    role: str                    # "system" | "user" | "assistant" | "tool"
    content: str
    tool_calls: list = field(default_factory=list)
    tool_call_id: str = ""
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

@dataclass
class ModelResponse:
    content: str
    tool_calls: list
    raw: Any = None

@dataclass
class TurnContext:
    messages: list
    turn: int = 0
    max_turns: int = 20
    stop_reason: str = "max_turns"
    state: dict = field(default_factory=dict)