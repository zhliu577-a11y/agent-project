# core/hooks.py —— 钩子网关：所有钩子插件的统一入口（类型化事件，异步）
import logging
from typing import Any

from core.types import ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)


class LifecycleHooks:
    """钩子插件基类：覆写需要关心的方法，不覆写的自动忽略。

    钩子插件放在 plugins/hooks/<name>/ 下，由插件加载器实例化后加入 HookGateway。
    """

    async def turn_start(self, ctx: TurnContext) -> None: ...
    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None: ...

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> bool:
        """返回 False 表示拒绝该工具调用（权限决策）。"""
        return True

    async def tool_after(
        self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool
    ) -> None: ...
    async def turn_end(self, ctx: TurnContext) -> None: ...


class HookGateway:
    """钩子网关：聚合所有钩子插件，内核只面向这一个网关。

    与 MCP 网关对应：内核把 turn/llm/tool 等生命周期事件交给本网关，
    由网关按注册顺序扇出给每个钩子插件，并负责异常隔离与决策汇总。
    """

    def __init__(self) -> None:
        self._hooks: list[LifecycleHooks] = []

    def add(self, hook: LifecycleHooks) -> None:
        self._hooks.append(hook)

    @property
    def count(self) -> int:
        return len(self._hooks)

    async def turn_start(self, ctx: TurnContext) -> None:
        for h in self._hooks:
            try:
                await h.turn_start(ctx)
            except Exception as exc:
                logger.exception("turn_start 钩子执行失败: %s", exc)

    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None:
        for h in self._hooks:
            try:
                await h.llm_response(ctx, resp)
            except Exception as exc:
                logger.exception("llm_response 钩子执行失败: %s", exc)

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> bool:
        """汇总所有钩子的决策：任何一个拒绝就拒绝；钩子异常按拒绝处理（安全侧默认拒绝）。"""
        for h in self._hooks:
            try:
                allowed = await h.tool_before(ctx, tool_call)
            except Exception as exc:
                logger.exception("tool_before 钩子执行失败: %s", exc)
                return False
            if not allowed:
                return False
        return True

    async def tool_after(
        self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool
    ) -> None:
        for h in self._hooks:
            try:
                await h.tool_after(ctx, tool_call, result, ok)
            except Exception as exc:
                logger.exception("tool_after 钩子执行失败: %s", exc)

    async def turn_end(self, ctx: TurnContext) -> None:
        for h in self._hooks:
            try:
                await h.turn_end(ctx)
            except Exception as exc:
                logger.exception("turn_end 钩子执行失败: %s", exc)
