# core/hooks.py —— 生命周期钩子（类型化事件）
import logging
from typing import Any

from core.types import ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)


class LifecycleHooks:
    """插件继承此类，覆写需要关心的方法。不覆写的自动忽略。"""

    def turn_start(self, ctx: TurnContext) -> None: ...
    def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None: ...
    def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> None: ...
    def tool_after(self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool) -> None: ...
    def turn_end(self, ctx: TurnContext) -> None: ...


class HookManager:
    """管理多个钩子插件，按注册顺序逐个调用，并隔离异常。"""

    def __init__(self) -> None:
        self._hooks: list[LifecycleHooks] = []

    def add(self, hook: LifecycleHooks) -> None:
        self._hooks.append(hook)

    def turn_start(self, ctx: TurnContext) -> None:
        for h in self._hooks:
            try:
                h.turn_start(ctx)
            except Exception as exc:
                logger.exception("turn_start 钩子执行失败: %s", exc)

    def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None:
        for h in self._hooks:
            try:
                h.llm_response(ctx, resp)
            except Exception as exc:
                logger.exception("llm_response 钩子执行失败: %s", exc)

    def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> None:
        for h in self._hooks:
            try:
                h.tool_before(ctx, tool_call)
            except Exception as exc:
                logger.exception("tool_before 钩子执行失败: %s", exc)

    def tool_after(self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool) -> None:
        for h in self._hooks:
            try:
                h.tool_after(ctx, tool_call, result, ok)
            except Exception as exc:
                logger.exception("tool_after 钩子执行失败: %s", exc)

    def turn_end(self, ctx: TurnContext) -> None:
        for h in self._hooks:
            try:
                h.turn_end(ctx)
            except Exception as exc:
                logger.exception("turn_end 钩子执行失败: %s", exc)
