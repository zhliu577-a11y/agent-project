"""Control-plane gateway for automatic long-term memory extraction."""

from __future__ import annotations

import logging

from core.events import Event, EventPublisher
from core.memory_extraction import (
    MemoryExtractionRequest,
    MemoryExtractionResult,
    MemoryExtractor,
)
from core.session import SessionSnapshot
from core.tracing import current_trace_id
from core.types import Message
from gateways.memory_gateway import MemoryGateway

logger = logging.getLogger(__name__)


class MemoryExtractionGateway:
    """Run one extractor and persist accepted candidates through MemoryGateway."""

    def __init__(
        self,
        extractor: MemoryExtractor,
        memory: MemoryGateway,
        *,
        events: EventPublisher | None = None,
    ) -> None:
        self._extractor = extractor
        self._memory = memory
        self._events = events

    async def _emit(self, name: str, **payload: object) -> None:
        if self._events is None:
            return
        await self._events.publish(
            Event(name=name, payload=dict(payload), trace_id=current_trace_id())
        )

    async def process_turn(
        self,
        snapshot: SessionSnapshot,
        new_messages: list[Message],
    ) -> list[str]:
        """Extract candidates without allowing memory failures to fail the turn."""
        identity = self._memory.identity
        request = MemoryExtractionRequest(
            session_id=snapshot.session_id,
            messages=list(snapshot.messages),
            new_messages=list(new_messages),
            revision=snapshot.metadata.revision,
            scope=identity.scope,
            owner_id=identity.owner_id,
            agent_id=identity.agent_id,
            tenant_id=identity.tenant_id,
            checkpoint=dict(snapshot.checkpoint or {}),
        )
        try:
            result = await self._extractor.extract(request)
            self._validate_result(result)
        except Exception as exc:
            logger.warning("memory extraction failed; continuing without memory writes: %s", exc)
            await self._emit("memory.extraction_failed", error=str(exc))
            return []

        written: list[str] = []
        for candidate in result.candidates:
            try:
                note = await self._memory.remember(
                    candidate.content,
                    candidate.tags,
                    kind=candidate.kind,
                    source=candidate.source,
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                    expires_at=candidate.expires_at,
                    metadata=candidate.metadata,
                )
            except Exception as exc:
                logger.warning("memory candidate write failed: %s", exc)
                continue
            written.append(note.id)

        await self._emit(
            "memory.extracted",
            session_id=snapshot.session_id,
            revision=snapshot.metadata.revision,
            candidates=len(result.candidates),
            written=len(written),
        )
        return written

    @staticmethod
    def _validate_result(result: MemoryExtractionResult) -> None:
        if not isinstance(result, MemoryExtractionResult):
            raise TypeError(
                f"memory extractor must return MemoryExtractionResult, got {type(result).__name__}"
            )
        for candidate in result.candidates:
            if not candidate.content.strip():
                raise ValueError("memory extractor returned an empty candidate")
            if not 0 <= candidate.confidence <= 1:
                raise ValueError("memory candidate confidence must be between 0 and 1")
            if not 0 <= candidate.importance <= 1:
                raise ValueError("memory candidate importance must be between 0 and 1")
