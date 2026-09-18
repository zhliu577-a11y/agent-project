"""Rolling extractive summary compaction for persisted session history."""

from __future__ import annotations

from typing import Any

from core.compaction import CompactionRequest, CompactionResult
from core.context import (
    build_extractive_summary,
    estimate_tokens,
    history_tokens,
    tail_window_view,
)
from core.types import Message


class RollingSummaryPolicy:
    """Summarize old messages once history exceeds a configurable threshold."""

    def __init__(
        self,
        *,
        trigger_tokens: int = 16000,
        keep_recent_tokens: int = 6000,
        summary_tokens: int = 2000,
    ) -> None:
        for name, value in (
            ("trigger_tokens", trigger_tokens),
            ("keep_recent_tokens", keep_recent_tokens),
            ("summary_tokens", summary_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._trigger_tokens = trigger_tokens
        self._keep_recent_tokens = keep_recent_tokens
        self._summary_tokens = summary_tokens

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        before_tokens = history_tokens(request.messages)
        if before_tokens <= self._trigger_tokens:
            return CompactionResult(
                messages=list(request.messages),
                metadata={
                    "strategy": "rolling-summary",
                    "reason": "below-threshold",
                    "beforeTokens": before_tokens,
                },
            )

        previous_summaries = [
            message
            for message in request.messages
            if message.metadata.get("compaction") == "summary"
        ]
        body = [
            message
            for message in request.messages
            if message.metadata.get("compaction") != "summary"
        ]
        kept, _ = tail_window_view(body, self._keep_recent_tokens)
        dropped = body[: len(body) - len(kept)]
        if not dropped:
            return CompactionResult(
                messages=list(request.messages),
                metadata={
                    "strategy": "rolling-summary",
                    "reason": "no-droppable-messages",
                    "beforeTokens": before_tokens,
                },
            )

        summary = build_extractive_summary(dropped, self._summary_tokens)
        if previous_summaries:
            previous = previous_summaries[-1].content.strip()
            if previous:
                summary = _merge_summaries(previous, summary, self._summary_tokens)
        if not summary:
            return CompactionResult(
                messages=list(request.messages),
                metadata={
                    "strategy": "rolling-summary",
                    "reason": "empty-summary",
                    "beforeTokens": before_tokens,
                },
            )

        summary_message = Message(
            role="system",
            content=summary,
            metadata={
                "compaction": "summary",
                "strategy": "rolling-summary",
                "sourceMessages": len(dropped),
            },
        )
        compacted = [summary_message, *kept]
        return CompactionResult(
            messages=compacted,
            compacted=True,
            summary=summary,
            metadata={
                "strategy": "rolling-summary",
                "reason": "over-threshold",
                "beforeTokens": before_tokens,
                "afterTokens": history_tokens(compacted),
                "droppedMessages": len(dropped),
            },
        )


def _merge_summaries(previous: str, current: str, max_tokens: int) -> str:
    header = "Earlier conversation summary:"
    body = previous
    if current:
        body = f"{body}\n\n{current}" if body else current
    if estimate_tokens(body) <= max_tokens:
        return body
    if estimate_tokens(header) >= max_tokens:
        return ""
    available = max_tokens - estimate_tokens(header)
    return f"{header}\n{body[:available]}"


def create_policy(plugin_dir, context=None) -> RollingSummaryPolicy:
    config: dict[str, Any] = dict(getattr(context, "config", {}) or {})
    return RollingSummaryPolicy(
        trigger_tokens=int(config.get("triggerTokens", 16000)),
        keep_recent_tokens=int(config.get("keepRecentTokens", 6000)),
        summary_tokens=int(config.get("summaryTokens", 2000)),
    )
