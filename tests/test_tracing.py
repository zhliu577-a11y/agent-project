# tests/test_tracing.py —— 日志追踪：trace id 的上下文隔离与过滤
import asyncio
import logging

import pytest

from core.tracing import TraceFilter, begin_trace, current_trace_id


@pytest.mark.asyncio
async def test_trace_id_is_isolated_per_task() -> None:
    async def worker(label: str) -> str | None:
        begin_trace(label)
        await asyncio.sleep(0)
        return current_trace_id()

    first, second = await asyncio.gather(worker("a"), worker("b"))
    assert (first, second) == ("a", "b")
    assert current_trace_id() is None  # 外层上下文不受子任务影响


def test_trace_filter_adds_field() -> None:
    record = logging.LogRecord("x", logging.INFO, "f", 1, "msg", None, None)
    trace_filter = TraceFilter()
    assert trace_filter.filter(record) is True
    assert record.trace == "-"

    begin_trace("abc123")
    assert trace_filter.filter(record) is True
    assert record.trace == "abc123"
