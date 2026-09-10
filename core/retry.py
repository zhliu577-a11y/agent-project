# core/retry.py —— 重试策略抽象：指数退避 + 可替换的“是否可重试”判定
import asyncio
import logging
from collections.abc import Awaitable, Callable

from core.errors import is_retryable

logger = logging.getLogger(__name__)


async def retry_async(
    factory: Callable[[], Awaitable],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    retryable: Callable[[BaseException], bool] | None = None,
):
    """以指数退避重试一个异步工厂；不可重试或耗尽次数则抛出最后一次异常。"""
    retryable = retryable or is_retryable
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except Exception as exc:  # noqa: BLE001 - 由 retryable 判定是否上抛
            last_error = exc
            is_last = attempt == attempts - 1
            if is_last or not retryable(exc):
                raise
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                "操作失败，%.2f 秒后第 %d/%d 次重试: %s", delay, attempt + 2, attempts, exc
            )
            await asyncio.sleep(delay)
    raise last_error  # pragma: no cover - attempts>=1 时不可达
