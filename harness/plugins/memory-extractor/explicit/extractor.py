"""Conservative rule-based extraction of explicit long-term memory."""

from __future__ import annotations

import re
from typing import Any

from core.memory_extraction import (
    MemoryCandidate,
    MemoryExtractionRequest,
    MemoryExtractionResult,
)

_RULES: tuple[tuple[re.Pattern[str], str, float], ...] = (
    (re.compile(r"^\s*(?:请)?记住\s*[:：,，]?\s*(?P<body>.+)$", re.IGNORECASE), "fact", 0.95),
    (
        re.compile(r"^\s*remember(?:\s+that)?\s*[:：,，]?\s*(?P<body>.+)$", re.IGNORECASE),
        "fact",
        0.95,
    ),
    (
        re.compile(r"^\s*我(?:更)?(?:喜欢|偏好|倾向于?|习惯)\s*(?P<body>.+)$"),
        "preference",
        0.8,
    ),
    (
        re.compile(r"^\s*I\s+prefer\s+(?P<body>.+)$", re.IGNORECASE),
        "preference",
        0.8,
    ),
    (
        re.compile(r"^\s*(?:以后|今后)\s*(?:都|请|要)?\s*(?P<body>.+)$"),
        "constraint",
        0.8,
    ),
    (
        re.compile(r"^\s*(?P<body>(?:always|never)\b.+)$", re.IGNORECASE),
        "constraint",
        0.8,
    ),
)


class ExplicitMemoryExtractor:
    """Extract only statements that explicitly ask the agent to remember a preference."""

    def __init__(self, *, max_candidates: int = 8, min_chars: int = 2) -> None:
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        if min_chars <= 0:
            raise ValueError("min_chars must be positive")
        self._max_candidates = max_candidates
        self._min_chars = min_chars

    async def extract(self, request: MemoryExtractionRequest) -> MemoryExtractionResult:
        candidates: list[MemoryCandidate] = []
        seen: set[str] = set()
        for message in request.new_messages:
            if message.role != "user":
                continue
            candidate = self._extract_message(message.content, message.id)
            if candidate is None:
                continue
            normalized = " ".join(candidate.content.split()).casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(candidate)
            if len(candidates) >= self._max_candidates:
                break
        return MemoryExtractionResult(
            candidates=candidates,
            metadata={"strategy": "explicit", "scannedMessages": len(request.new_messages)},
        )

    def _extract_message(self, content: str, message_id: str) -> MemoryCandidate | None:
        text = content.strip()
        if not text:
            return None
        for pattern, kind, confidence in _RULES:
            match = pattern.match(text)
            if match is None:
                continue
            body = match.group("body").strip(" \t\r\n:：,，。")
            if len(body) < self._min_chars:
                return None
            return MemoryCandidate(
                content=body,
                tags=["auto", kind],
                kind=kind,
                source="extracted",
                confidence=confidence,
                importance=0.7 if kind == "constraint" else 0.6,
                metadata={"evidenceMessageIds": [message_id], "extractor": "explicit"},
            )
        return None


def create_extractor(plugin_dir, context=None) -> ExplicitMemoryExtractor:
    config: dict[str, Any] = dict(getattr(context, "config", {}) or {})
    return ExplicitMemoryExtractor(
        max_candidates=int(config.get("maxCandidates", 8)),
        min_chars=int(config.get("minChars", 2)),
    )
