# core/errors.py —— 统一错误分类 + 边界翻译
#
# 三条路径：
#   1. 内部代码明确知道语义时，直接 raise ConfigError/ModelError/...
#   2. 外部异常（openai/sqlite/OSError...）在模块边界用 classify_error 翻译成内部类别
#   3. retry/日志/接口/模型反馈统一消费 category，不再各自判断第三方类型
import asyncio
import functools
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from typing import Any


class AgentError(Exception):
    """Harness 领域错误基类；category 用于区分来源与重试策略。"""

    category = "agent"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class ConfigError(AgentError, ValueError):
    category = "config"


class PluginError(AgentError, ValueError):
    category = "plugin"


class ModelError(AgentError):
    category = "model"


class ToolError(AgentError):
    category = "tool"


class RetryableError(AgentError):
    """瞬时可重试错误标记（连接抖动、临时 429/5xx 等）。"""

    category = "retryable"


# 内核固定分类：插件只能声明这些类别，不得自定义体系
AGENT_CATEGORIES = ("agent", "config", "plugin", "model", "tool", "retryable")
CATEGORY_TYPES: dict[str, type[AgentError]] = {
    "agent": AgentError,
    "config": ConfigError,
    "plugin": PluginError,
    "model": ModelError,
    "tool": ToolError,
    "retryable": RetryableError,
}


class DeclaredPluginError(AgentError):
    """插件按清单声明的错误码抛出的领域错误（携带 code / hint）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: str = "plugin",
        hint: str = "",
        plugin: str | None = None,
    ) -> None:
        super().__init__(message, category=category)
        self.code = code
        self.hint = hint
        self.plugin = plugin


def error_from_category(category: str, message: str) -> AgentError:
    """按 category 构造对应类别的异常（未知类别退回 AgentError）。"""
    return CATEGORY_TYPES.get(category, AgentError)(message, category=category)


def resolve_declared_error(
    exc: BaseException,
    declarations: Mapping[str, Any],
    *,
    plugin: str | None = None,
) -> BaseException:
    """按插件清单声明补全 DeclaredPluginError 的 category/hint。

    - 不是 DeclaredPluginError，或 code 未声明 → 原样返回；
    - 命中声明 → 返回补全后的新异常（message 不变），调用方负责 from exc。
    """
    if not isinstance(exc, DeclaredPluginError):
        return exc
    declared = declarations.get(exc.code)
    if declared is None:
        return exc
    return DeclaredPluginError(
        exc.code,
        str(exc),
        category=declared.category,
        hint=declared.hint,
        plugin=plugin or exc.plugin,
    )


def is_retryable(exc: BaseException) -> bool:
    """判断一个异常是否值得重试（先做边界分类再判断）。"""
    classified = classify_error(exc)
    return isinstance(classified, RetryableError) or (
        getattr(classified, "category", None) == "retryable"
    )


# 常见的“可重试 HTTP 状态”：408/429 与 5xx
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_CONTROL_EXCEPTIONS = (asyncio.CancelledError, KeyboardInterrupt, SystemExit)


def classify_error(exc: BaseException) -> BaseException:
    """把边界外的原始异常翻译成本体系类别；不认识的原样放行。

    设计要点：
    - 白名单式映射，宁可放行也不误判（避免错误地自动重试）；
    - CancelledError / KeyboardInterrupt / SystemExit 原样透传；
    - 用鸭子类型识别第三方错误（status_code、类名），core 不依赖 openai 等库。
    """
    if isinstance(exc, _CONTROL_EXCEPTIONS) or isinstance(exc, AgentError):
        return exc

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in _RETRYABLE_STATUS:
            return RetryableError(f"上游返回 {status}: {exc}")
        return ModelError(f"上游返回 {status}: {exc}")

    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        return RetryableError(str(exc))
    if isinstance(exc, ConnectionError) or "Connection" in name:
        return RetryableError(str(exc))
    if isinstance(exc, sqlite3.Error):
        return ToolError(str(exc))
    if isinstance(exc, OSError):
        return PluginError(str(exc))
    return exc


def translate_error(
    exc: BaseException,
    *,
    context: str,
    fallback: type[AgentError] = PluginError,
) -> AgentError:
    """在边界处生成带上下文的内部异常（配合 raise ... from exc 保留原始堆栈）。"""
    classified = classify_error(exc)
    if isinstance(classified, AgentError):
        return type(classified)(f"{context}: {classified}", category=classified.category)
    return fallback(f"{context}: {exc}")


def boundary(
    context: str, fallback: type[AgentError] = PluginError
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """装饰异步边界方法：内部抛出的异常统一翻译成带上下文的类别异常。"""

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                raise translate_error(exc, context=context, fallback=fallback) from exc

        return wrapper

    return decorator
