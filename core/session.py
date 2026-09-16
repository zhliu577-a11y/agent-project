"""Session storage contracts for short-term conversation memory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.errors import AgentError
from core.types import Message


class SessionConflictError(AgentError):
    """The caller tried to commit against a stale session revision."""


def validate_session_id(session_id: str) -> str:
    """Validate a portable, path-safe session identifier."""
    if not isinstance(session_id, str):
        raise ValueError("session_id must be a string")
    value = session_id.strip()
    if not value:
        raise ValueError("session_id must not be empty")
    if len(value) > 128:
        raise ValueError("session_id must be at most 128 characters")
    if value in {".", ".."}:
        raise ValueError("session_id must not be '.' or '..'")
    if "/" in value or "\\" in value:
        raise ValueError("session_id must not contain path separators")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("session_id must not contain control characters")
    if any(char in '<>:"|?*' for char in value):
        raise ValueError("session_id must not contain reserved filename characters")
    if value.endswith((".", " ")):
        raise ValueError("session_id must not end with a dot or space")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class SessionMetadata:
    """Revision and descriptive metadata stored alongside a session."""

    revision: int = 0
    message_count: int = 0
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    title: str = ""
    owner: str = ""
    parent_id: str | None = None
    labels: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "message_count": self.message_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "owner": self.owner,
            "parent_id": self.parent_id,
            "labels": list(self.labels),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SessionMetadata:
        if not raw:
            return cls()
        return cls(
            revision=max(0, int(raw.get("revision", 0))),
            message_count=max(0, int(raw.get("message_count", 0))),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            title=str(raw.get("title", "")),
            owner=str(raw.get("owner", "")),
            parent_id=(str(raw["parent_id"]) if raw.get("parent_id") is not None else None),
            labels=tuple(str(item) for item in raw.get("labels", [])),
            extra=dict(raw.get("extra", {})),
        )

    def advanced(
        self,
        *,
        message_count: int,
        title: str | None = None,
        owner: str | None = None,
        parent_id: str | None = None,
        labels: tuple[str, ...] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SessionMetadata:
        return SessionMetadata(
            revision=self.revision + 1,
            message_count=max(0, message_count),
            created_at=self.created_at,
            updated_at=_now(),
            title=self.title if title is None else title,
            owner=self.owner if owner is None else owner,
            parent_id=self.parent_id if parent_id is None else parent_id,
            labels=self.labels if labels is None else labels,
            extra=self.extra if extra is None else extra,
        )


@dataclass(frozen=True)
class SessionSnapshot:
    """A consistent read view of one session."""

    session_id: str
    messages: list[Message]
    metadata: SessionMetadata
    checkpoint: dict[str, Any] | None = None


def ensure_revision(expected: int | None, current: int) -> None:
    """Reject stale writes when a caller requested optimistic concurrency."""
    if expected is not None and expected != current:
        raise SessionConflictError(
            f"session revision conflict: expected {expected}, current {current}"
        )


class SessionStore(ABC):
    """Persistence contract for short-term session messages.

    ``load`` and ``save`` remain the compatibility floor for v1 plugins.
    Built-in stores override the richer operations so the gateway can provide
    optimistic concurrency, append semantics, and atomic replacement.
    """

    supports_revisioning = False

    @abstractmethod
    async def load(self, session_id: str) -> list[Message]:
        """Read all messages; return an empty list when the session is absent."""
        ...

    @abstractmethod
    async def save(self, session_id: str, messages: list[Message]) -> None:
        """Replace all messages for compatibility with the original contract."""
        ...

    @abstractmethod
    async def load_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        """Read the latest run checkpoint, or ``None``."""
        ...

    @abstractmethod
    async def save_checkpoint(self, session_id: str, snapshot: dict[str, Any]) -> None:
        """Persist a JSON-safe run checkpoint."""
        ...

    @abstractmethod
    async def delete_checkpoint(self, session_id: str) -> None:
        """Delete the latest run checkpoint."""
        ...

    async def get(self, session_id: str, limit: int | None = None) -> list[Message]:
        """Read all messages or only the newest ``limit`` messages."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        messages = await self.load(session_id)
        if limit is None or limit == 0:
            return messages
        return messages[-limit:]

    async def append(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        """Append messages using the compatibility load/save pair."""
        metadata = await self.load_metadata(session_id)
        if self.supports_revisioning:
            ensure_revision(expected_revision, metadata.revision)
        current = await self.load(session_id)
        updated = [*current, *messages]
        await self.save(session_id, updated)
        next_metadata = metadata.advanced(message_count=len(updated))
        await self.save_metadata(session_id, next_metadata)
        return next_metadata

    async def replace(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        """Replace messages using the compatibility ``save`` operation."""
        metadata = await self.load_metadata(session_id)
        if self.supports_revisioning:
            ensure_revision(expected_revision, metadata.revision)
        await self.save(session_id, messages)
        next_metadata = metadata.advanced(message_count=len(messages))
        await self.save_metadata(session_id, next_metadata)
        return next_metadata

    async def clear(self, session_id: str) -> SessionMetadata:
        """Clear messages and checkpoint state."""
        metadata = await self.load_metadata(session_id)
        await self.save(session_id, [])
        await self.delete_checkpoint(session_id)
        next_metadata = metadata.advanced(message_count=0)
        await self.save_metadata(session_id, next_metadata)
        return next_metadata

    async def load_metadata(self, session_id: str) -> SessionMetadata:
        """Return revision metadata; compatibility stores report revision zero."""
        return SessionMetadata()

    async def save_metadata(self, session_id: str, metadata: SessionMetadata) -> None:
        """Persist revision metadata when a backend supports it."""
        return None

    async def snapshot(self, session_id: str, limit: int | None = None) -> SessionSnapshot:
        """Read messages, metadata, and checkpoint as one gateway-facing view."""
        messages = await self.get(session_id, limit=limit)
        metadata = await self.load_metadata(session_id)
        checkpoint = await self.load_checkpoint(session_id)
        return SessionSnapshot(
            session_id=session_id,
            messages=messages,
            metadata=metadata,
            checkpoint=checkpoint,
        )
