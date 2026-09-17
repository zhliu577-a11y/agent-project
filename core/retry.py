"""Host-owned retry execution with a replaceable decision policy."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from core.errors import classify_error, is_retryable
from core.events import Event, EventPublisher
from core.tool import Tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryError:
    """Normalized error information exposed to a retry policy."""

    category: str
    message: str
    code: str | None = None
    hint: str = ""


@dataclass(frozen=True)
class RetryRequest:
    """One failed attempt offered to a retry policy."""

    operation: str
    component: str
    attempt: int
    max_attempts: int
    error: RetryError
    retry_safe: bool
    stream_started: bool = False
    elapsed: float = 0.0


@dataclass(frozen=True)
class RetryDecision:
    """A policy recommendation; the host may still reject it for safety."""

    retry: bool
    delay: float = 0.0
    reason: str = ""


@runtime_checkable
class RetryPolicy(Protocol):
    """Replaceable retry decision contract."""

    async def decide(self, request: RetryRequest) -> RetryDecision:
        """Return whether one failed attempt should be retried."""
        ...


RecoveryCallback = Callable[[RetryRequest], Awaitable[None]]


class RetryingTool(Tool):
    """Apply the host retry executor to one retry-safe plugin tool."""

    def __init__(
        self,
        tool: Tool,
        retry: RetryExecutor,
        *,
        operation: str = "tool.call",
        component: str | None = None,
        recover: RecoveryCallback | None = None,
    ) -> None:
        self._tool = tool
        self._retry = retry
        self._operation = operation
        self._component = component or tool.name
        self._recover = recover

    @property
    def name(self) -> str:
        return self._tool.name

    @property
    def description(self) -> str:
        return self._tool.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._tool.parameters

    async def execute(self, **kwargs: Any) -> Any:
        async def _invoke() -> Any:
            return await self._tool.execute(**kwargs)

        return await self._retry.execute(
            _invoke,
            operation=self._operation,
            component=self._component,
            retry_safe=True,
            recover=self._recover,
        )


class RetryExecutor:
    """Execute retries while enforcing host-owned safety limits."""

    def __init__(
        self,
        policies: Mapping[str, RetryPolicy],
        *,
        default_policy: str | None,
        routes: Mapping[str, str] | None = None,
        max_attempts: int = 3,
        max_delay: float = 2.0,
        total_timeout: float = 30.0,
        events: EventPublisher | None = None,
    ) -> None:
        if not policies:
            raise ValueError("RetryExecutor requires at least one policy")
        if default_policy is not None and default_policy not in policies:
            raise ValueError(f"unknown default retry policy: {default_policy!r}")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if (
            isinstance(max_delay, bool)
            or not isinstance(max_delay, (int, float))
            or not math.isfinite(max_delay)
            or max_delay < 0
        ):
            raise ValueError("max_delay must be a finite non-negative number")
        if (
            isinstance(total_timeout, bool)
            or not isinstance(total_timeout, (int, float))
            or not math.isfinite(total_timeout)
            or total_timeout <= 0
        ):
            raise ValueError("total_timeout must be a finite positive number")

        parsed_routes = dict(routes or {})
        for operation, name in parsed_routes.items():
            if not isinstance(operation, str) or not operation.strip():
                raise ValueError("retry route operations must be non-empty strings")
            if name not in policies:
                raise ValueError(f"retry route {operation!r} references unknown policy {name!r}")

        self._policies = dict(policies)
        self._default_policy = default_policy
        self._routes = parsed_routes
        self._max_attempts = max_attempts
        self._max_delay = float(max_delay)
        self._total_timeout = float(total_timeout)
        self._events = events

    @property
    def default_policy(self) -> str | None:
        return self._default_policy

    @property
    def routes(self) -> Mapping[str, str]:
        return dict(self._routes)

    def policy_for(self, operation: str) -> tuple[str, RetryPolicy]:
        """Resolve the selected policy for an operation."""
        name = self._routes.get(operation, self._default_policy)
        if name is None:
            raise KeyError(operation)
        return name, self._policies[name]

    async def execute(
        self,
        factory: Callable[[], Awaitable],
        *,
        operation: str,
        component: str,
        retry_safe: bool,
        stream_started: Callable[[], bool] | None = None,
        recover: RecoveryCallback | None = None,
    ):
        """Run one operation under the selected policy and host safety limits."""
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("operation must be a non-empty string")
        if not isinstance(component, str) or not component.strip():
            raise ValueError("component must be a non-empty string")

        started = time.monotonic()
        retry_count = 0
        last_error: BaseException | None = None

        for attempt in range(1, self._max_attempts + 1):
            elapsed = time.monotonic() - started
            remaining = self._total_timeout - elapsed
            if remaining <= 0:
                last_error = TimeoutError(f"retry deadline exceeded for {component}/{operation}")
                break
            try:
                async with asyncio.timeout(remaining):
                    result = await factory()
            except Exception as exc:
                last_error = exc
                elapsed = time.monotonic() - started
                if elapsed >= self._total_timeout:
                    last_error = TimeoutError(
                        f"retry deadline exceeded for {component}/{operation}"
                    )
                    break
                if attempt >= self._max_attempts:
                    break

                streamed = bool(stream_started()) if stream_started is not None else False
                request = self._request(
                    operation=operation,
                    component=component,
                    attempt=attempt,
                    error=exc,
                    retry_safe=retry_safe,
                    stream_started=streamed,
                    elapsed=elapsed,
                )

                if not request.retry_safe or request.stream_started:
                    raise

                decision = await self._decide(operation, request)
                if decision is None or not decision.retry:
                    raise
                try:
                    delay = self._validate_delay(decision.delay)
                except ValueError as invalid:
                    raise exc from invalid
                if request.elapsed + delay >= self._total_timeout:
                    logger.warning(
                        "retry delay would exceed deadline for %s/%s",
                        component,
                        operation,
                    )
                    raise

                retry_count += 1
                await self._emit(
                    "retry.scheduled",
                    operation=operation,
                    component=component,
                    attempt=attempt + 1,
                    delay=delay,
                    reason=decision.reason,
                    error=request.error,
                )

                if recover is not None:
                    remaining = self._total_timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        logger.warning(
                            "retry recovery deadline exceeded for %s/%s",
                            component,
                            operation,
                        )
                        raise exc
                    try:
                        async with asyncio.timeout(remaining):
                            await recover(request)
                    except TimeoutError as recovery_error:
                        logger.warning(
                            "retry recovery timed out for %s/%s",
                            component,
                            operation,
                        )
                        raise exc from recovery_error
                    except Exception as recovery_error:
                        logger.exception(
                            "retry recovery failed for %s/%s",
                            component,
                            operation,
                        )
                        raise exc from recovery_error

                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            else:
                if retry_count:
                    await self._emit(
                        "retry.succeeded",
                        operation=operation,
                        component=component,
                        attempts=attempt,
                        retries=retry_count,
                    )
                return result

        if retry_count:
            await self._emit(
                "retry.exhausted",
                operation=operation,
                component=component,
                attempts=self._max_attempts,
                retries=retry_count,
                error=self._error_info(last_error),
            )
        assert last_error is not None
        raise last_error

    async def _decide(
        self,
        operation: str,
        request: RetryRequest,
    ) -> RetryDecision | None:
        try:
            _, policy = self.policy_for(operation)
        except KeyError:
            return None
        try:
            result = policy.decide(request)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logger.exception(
                "retry policy failed for %s/%s; retry disabled",
                request.component,
                operation,
            )
            return None
        if not isinstance(result, RetryDecision):
            logger.error(
                "retry policy returned %s for %s/%s; retry disabled",
                type(result).__name__,
                request.component,
                operation,
            )
            return None
        return result

    def _validate_delay(self, delay: object) -> float:
        if (
            isinstance(delay, bool)
            or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or delay < 0
        ):
            raise ValueError(f"invalid retry delay: {delay!r}")
        return min(float(delay), self._max_delay)

    def _request(
        self,
        *,
        operation: str,
        component: str,
        attempt: int,
        error: BaseException,
        retry_safe: bool,
        stream_started: bool,
        elapsed: float,
    ) -> RetryRequest:
        return RetryRequest(
            operation=operation,
            component=component,
            attempt=attempt,
            max_attempts=self._max_attempts,
            error=self._error_info(error),
            retry_safe=retry_safe,
            stream_started=stream_started,
            elapsed=elapsed,
        )

    @staticmethod
    def _error_info(error: BaseException | None) -> RetryError:
        if error is None:
            return RetryError(category="agent", message="unknown error")
        classified = classify_error(error)
        return RetryError(
            category=getattr(classified, "category", type(classified).__name__),
            message=str(classified),
            code=getattr(classified, "code", None),
            hint=getattr(classified, "hint", ""),
        )

    async def _emit(self, name: str, **payload: object) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(Event(name=name, payload=dict(payload)))
        except Exception:
            logger.exception("retry event publish failed: %s", name)


async def retry_async(
    factory: Callable[[], Awaitable],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    retryable: Callable[[BaseException], bool] | None = None,
):
    """Backward-compatible simple retry helper for non-plugin call sites."""
    retryable = retryable or is_retryable
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - delegated retry classifier
            last_error = exc
            is_last = attempt == attempts - 1
            if is_last or not retryable(exc):
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                "操作失败，%.2f 秒后第 %d/%d 次重试: %s",
                delay,
                attempt + 2,
                attempts,
                exc,
            )
            await asyncio.sleep(delay)
    raise last_error  # pragma: no cover - attempts>=1 时不可达
