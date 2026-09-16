"""Compaction policy contracts for persisted short-term memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.types import Message


@dataclass(frozen=True)
class CompactionRequest:
    """One snapshot offered to a compaction policy before it is persisted."""

    session_id: str
    messages: list[Message]
    max_tokens: int
    revision: int
    checkpoint: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompactionResult:
    """A policy decision and the replacement history it recommends."""

    messages: list[Message]
    compacted: bool = False
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class CompactionPolicy(Protocol):
    """Replaceable strategy for compacting persisted conversation history."""

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        """Decide whether to compact and return the replacement history."""
        ...
