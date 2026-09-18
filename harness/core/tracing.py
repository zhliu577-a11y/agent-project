# core/tracing.py —— 日志追踪：trace id 与结构化日志格式
#
# 用 contextvars 让“一次请求/一轮对话”共享同一个 trace id，
# 日志行自动带上 trace=xxx，便于把多行日志串成一条链路。
import contextvars
import logging
import uuid

_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [trace=%(trace)s] %(message)s"


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


def begin_trace(trace_id: str | None = None) -> str:
    """开始一条追踪：设置并返回 trace id（同一个 asyncio 任务内共享）。"""
    trace_id = trace_id or new_trace_id()
    _TRACE_ID.set(trace_id)
    return trace_id


def current_trace_id() -> str | None:
    return _TRACE_ID.get()


class TraceFilter(logging.Filter):
    """给每条日志记录补上 trace 字段（没有则填 '-'）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace = _TRACE_ID.get() or "-"
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """配置根日志：控制台 handler + trace 过滤器（可重复调用，幂等）。"""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, "_codex_trace_handler", False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(TraceFilter())
    handler._codex_trace_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
