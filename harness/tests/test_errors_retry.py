# tests/test_errors_retry.py —— 错误分类与重试策略
import asyncio
from typing import Any

import pytest

from core.errors import RetryableError, is_retryable
from core.retry import RetryDecision, RetryExecutor, RetryingTool, RetryRequest, retry_async
from core.tool import Tool


class _Policy:
    def __init__(self, *, retry: bool = True, delay: float = 0.0) -> None:
        self.retry = retry
        self.delay = delay
        self.requests: list[RetryRequest] = []

    async def decide(self, request: RetryRequest) -> RetryDecision:
        self.requests.append(request)
        return RetryDecision(retry=self.retry, delay=self.delay, reason="test")


def test_is_retryable_classifies_timeout_and_retryable() -> None:
    assert is_retryable(TimeoutError("net")) is True
    assert is_retryable(RetryableError("boom")) is True
    assert is_retryable(ValueError("bad")) is False


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_failures() -> None:
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("瞬时抖动")
        return "ok"

    result = await retry_async(flaky, attempts=3, base_delay=0.0, max_delay=0.0)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_permanent_errors() -> None:
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("不可重试")

    with pytest.raises(ValueError):
        await retry_async(boom, attempts=3, base_delay=0.0, max_delay=0.0)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_exhausts_attempts() -> None:
    calls = {"n": 0}

    async def always_fail():
        calls["n"] += 1
        raise RetryableError("一直失败")

    with pytest.raises(RetryableError):
        await retry_async(always_fail, attempts=2, base_delay=0.0, max_delay=0.0)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_policy_executor_retries_and_runs_recovery() -> None:
    policy = _Policy()
    executor = RetryExecutor(
        {"test": policy},
        default_policy="test",
        max_attempts=3,
        max_delay=0,
        total_timeout=1,
    )
    calls = {"n": 0}
    recovered = 0

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableError("transient")
        return "ok"

    async def recover(request: RetryRequest) -> None:
        nonlocal recovered
        recovered += 1

    result = await executor.execute(
        flaky,
        operation="demo.call",
        component="demo",
        retry_safe=True,
        recover=recover,
    )

    assert result == "ok"
    assert calls["n"] == 3
    assert recovered == 2
    assert [request.attempt for request in policy.requests] == [1, 2]


@pytest.mark.asyncio
async def test_policy_executor_hard_denies_unsafe_or_streamed_retry() -> None:
    policy = _Policy()
    executor = RetryExecutor(
        {"test": policy},
        default_policy="test",
        max_attempts=3,
        max_delay=0,
        total_timeout=1,
    )
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        raise RetryableError("transient")

    with pytest.raises(RetryableError):
        await executor.execute(
            fail,
            operation="demo.unsafe",
            component="demo",
            retry_safe=False,
        )
    assert calls == 1
    assert policy.requests == []

    calls = 0
    with pytest.raises(RetryableError):
        await executor.execute(
            fail,
            operation="demo.stream",
            component="demo",
            retry_safe=True,
            stream_started=lambda: True,
        )
    assert calls == 1
    assert policy.requests == []


@pytest.mark.asyncio
async def test_policy_executor_without_default_or_route_does_not_retry() -> None:
    policy = _Policy()
    executor = RetryExecutor(
        {"test": policy},
        default_policy=None,
        max_attempts=3,
        max_delay=0,
        total_timeout=1,
    )
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        raise RetryableError("transient")

    with pytest.raises(RetryableError):
        await executor.execute(
            fail,
            operation="unrouted.call",
            component="demo",
            retry_safe=True,
        )

    assert calls == 1
    assert policy.requests == []


@pytest.mark.asyncio
async def test_policy_executor_enforces_total_deadline() -> None:
    executor = RetryExecutor(
        {"test": _Policy()},
        default_policy="test",
        max_attempts=3,
        max_delay=0,
        total_timeout=0.02,
    )
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)

    with pytest.raises(TimeoutError, match="retry deadline exceeded"):
        await executor.execute(
            slow,
            operation="demo.slow",
            component="demo",
            retry_safe=True,
        )

    assert calls == 1


class _CounterTool(Tool):
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "counter"

    @property
    def description(self) -> str:
        return "test tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            raise RetryableError("transient")
        return kwargs["value"]


@pytest.mark.asyncio
async def test_retrying_tool_uses_host_executor() -> None:
    executor = RetryExecutor(
        {"test": _Policy()},
        default_policy="test",
        max_attempts=2,
        max_delay=0,
        total_timeout=1,
    )
    tool = _CounterTool()
    wrapped = RetryingTool(tool, executor, component="tool:text")

    assert wrapped.name == "counter"
    assert await wrapped.execute(value="ok") == "ok"
    assert tool.calls == 2
