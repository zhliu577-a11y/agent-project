"""Retry only classified transient failures under host safety limits."""

from __future__ import annotations

from core.retry import RetryDecision, RetryPolicy, RetryRequest


class TransientRetryPolicy(RetryPolicy):
    def __init__(
        self,
        *,
        base_delay: float = 0.2,
        max_delay: float = 2.0,
        categories: tuple[str, ...] = ("retryable",),
    ) -> None:
        if base_delay < 0:
            raise ValueError("base_delay must be non-negative")
        if max_delay < 0:
            raise ValueError("max_delay must be non-negative")
        if not categories:
            raise ValueError("categories must not be empty")
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._categories = frozenset(categories)

    async def decide(self, request: RetryRequest) -> RetryDecision:
        if request.stream_started:
            return RetryDecision(retry=False, reason="streaming has started")
        if not request.retry_safe:
            return RetryDecision(retry=False, reason="operation is not retry-safe")
        if request.error.category not in self._categories:
            return RetryDecision(
                retry=False,
                reason=f"error category {request.error.category!r} is not retryable",
            )
        delay = min(
            self._base_delay * (2 ** max(request.attempt - 1, 0)),
            self._max_delay,
        )
        return RetryDecision(
            retry=True,
            delay=delay,
            reason=f"transient error category {request.error.category!r}",
        )


def create_policy(plugin_dir, context=None) -> RetryPolicy:
    del plugin_dir
    config = dict(getattr(context, "config", {}) or {})
    raw_categories = config.get("categories", ["retryable"])
    if not isinstance(raw_categories, list) or not all(
        isinstance(category, str) and category.strip() for category in raw_categories
    ):
        raise ValueError("retry-policy transient 'categories' must be a string array")
    return TransientRetryPolicy(
        base_delay=float(config.get("baseDelay", 0.2)),
        max_delay=float(config.get("maxDelay", 2.0)),
        categories=tuple(category.strip() for category in raw_categories),
    )
