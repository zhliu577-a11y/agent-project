"""Contracts for converting committed turns into long-term memory candidates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from core.types import Message


@dataclass(frozen=True)
class MemoryExtractionRequest:
    """One committed turn offered to a memory extractor."""

    session_id: str
    messages: list[Message]
    new_messages: list[Message]
    revision: int
    scope: str = "user"
    owner_id: str = ""
    agent_id: str = ""
    tenant_id: str = ""
    checkpoint: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryCandidate:
    """Durable memory proposed by an extraction policy."""

    content: str
    tags: list[str] = field(default_factory=list)
    kind: str = "fact"
    source: str = "extracted"
    confidence: float = 0.7
    importance: float = 0.5
    expires_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryExtractionResult:
    """Candidates and diagnostics returned by one extraction pass."""

    candidates: list[MemoryCandidate] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MemoryExtractor(Protocol):
    """Replaceable policy for extracting semantic memory from a turn."""

    async def extract(self, request: MemoryExtractionRequest) -> MemoryExtractionResult:
        """Return zero or more memory candidates for the supplied turn."""
        ...
