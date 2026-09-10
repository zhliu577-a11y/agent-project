# core/errors.py —— 统一错误分类：给异常打“类别”标签，便于策略与日志处理


class AgentError(Exception):
    """Harness 领域错误基类；category 用于区分来源与重试策略。"""

    category = "agent"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class ConfigError(AgentError):
    category = "config"


class PluginError(AgentError):
    category = "plugin"


class ModelError(AgentError):
    category = "model"


class ToolError(AgentError):
    category = "tool"


class RetryableError(AgentError):
    """瞬时可重试错误标记（连接抖动、临时 429/5xx 等）。"""

    category = "retryable"


def is_retryable(exc: BaseException) -> bool:
    """判断一个异常是否值得重试：显式 RetryableError 或网络超时。"""
    return isinstance(exc, RetryableError) or isinstance(exc, TimeoutError)
