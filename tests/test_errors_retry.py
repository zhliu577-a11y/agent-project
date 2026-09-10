# tests/test_errors_retry.py —— 错误分类与重试策略
import pytest

from core.errors import RetryableError, is_retryable
from core.retry import retry_async


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
