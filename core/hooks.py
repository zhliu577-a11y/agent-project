# core/hooks.py —— 钩子网关：所有钩子插件的统一入口（类型化事件，异步）
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from core.types import ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)

# 钩子对“是否执行工具”的表态：
#   allow —— 放行；ask —— 需要用户确认；deny —— 拒绝。
# 网关按 deny > ask > allow 折叠多个钩子的决策。
HookDecision = Literal["allow", "ask", "deny"]

ConfirmFn = Callable[[TurnContext, ToolCall], Awaitable[bool]]


class LifecycleHooks:
    """钩子插件基类：覆写需要关心的方法，不覆写的自动忽略。

    钩子插件放在 plugins/hooks/<name>/ 下，由插件加载器实例化后加入 HookGateway。
    """

    async def turn_start(self, ctx: TurnContext) -> None: ...
    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None: ...

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> HookDecision:
        """对该工具调用表态：allow（放行）/ ask（请用户确认）/ deny（拒绝）。"""
        return "allow"

    async def tool_after(
        self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool
    ) -> None: ...
    async def turn_end(self, ctx: TurnContext) -> None: ...


async def _default_confirm(ctx: TurnContext, tool_call: ToolCall) -> bool:
    """网关默认的 ask 确认：交互提示必须走 input（用户要看得见并回答）。"""
    logger.info("请求用户确认工具调用: %s", tool_call.name)
    answer = (
        input(f"[权限] 是否允许调用 {tool_call.name}(参数: {tool_call.arguments})? [y/N]: ")
        .strip()
        .lower()
    )
    return answer in {"y", "yes"}


class HookGateway:
    """钩子网关：聚合所有钩子插件，内核只面向这一个网关。

    与 MCP 网关对应：内核把 turn/llm/tool 等生命周期事件交给本网关，
    由网关按（priority, 注册顺序）扇出给每个钩子插件，并负责异常隔离与决策汇总。

    执行顺序设计：
    - priority 越小越先执行；相同 priority 保持 add 的先后顺序（稳定、可预测）；
    - 权限/安全类闸门建议用较小的 priority（先表态），记录/注入类默认值即可；
    - tool_before 不做“首个拒绝即短路”，而是让所有钩子表态后折叠
      （deny > ask > allow），便于审计钩子看到完整尝试；
    - 钩子抛异常视为该钩子表态 deny（安全侧默认拒绝），但不阻断其余钩子表态；
    - 折叠结果为 ask 时，网关只向用户确认一次（注入 confirm 可替换交互实现）。
    """

    def __init__(self) -> None:
        self._hooks: list[tuple[int, int, LifecycleHooks]] = []

    def add(self, hook: LifecycleHooks, priority: int = 0) -> None:
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"priority 必须是整数，收到: {priority!r}")
        self._hooks.append((priority, len(self._hooks), hook))

    @property
    def count(self) -> int:
        return len(self._hooks)

    def _ordered(self) -> list[tuple[int, int, LifecycleHooks]]:
        return sorted(self._hooks)

    async def turn_start(self, ctx: TurnContext) -> None:
        for _, _, hook in self._ordered():
            try:
                await hook.turn_start(ctx)
            except Exception as exc:
                logger.exception("turn_start 钩子执行失败: %s", exc)

    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None:
        for _, _, hook in self._ordered():
            try:
                await hook.llm_response(ctx, resp)
            except Exception as exc:
                logger.exception("llm_response 钩子执行失败: %s", exc)

    async def tool_before(
        self,
        ctx: TurnContext,
        tool_call: ToolCall,
        confirm: ConfirmFn | None = None,
    ) -> bool:
        """让全部钩子表态并按 deny > ask > allow 折叠，返回最终是否放行。"""
        decision: HookDecision = "allow"
        for _, _, hook in self._ordered():
            try:
                vote = await hook.tool_before(ctx, tool_call)
            except Exception as exc:
                logger.exception("tool_before 钩子执行失败，按拒绝处理: %s", exc)
                vote = "deny"
            if vote not in ("allow", "ask", "deny"):
                logger.warning(
                    "钩子 %s 返回了非法决策 %r，按拒绝处理",
                    type(hook).__name__,
                    vote,
                )
                vote = "deny"
            if vote == "deny":
                decision = "deny"
            elif decision == "allow" and vote == "ask":
                decision = "ask"

        if decision == "deny":
            logger.warning("工具调用被钩子网关拦截: %s", tool_call.name)
            return False
        if decision == "ask":
            asker = confirm if confirm is not None else _default_confirm
            try:
                return await asker(ctx, tool_call)
            except Exception as exc:
                logger.exception("ask 确认执行失败，按拒绝处理: %s", exc)
                return False
        return True

    async def tool_after(
        self, ctx: TurnContext, tool_call: ToolCall, result: Any, ok: bool
    ) -> None:
        for _, _, hook in self._ordered():
            try:
                await hook.tool_after(ctx, tool_call, result, ok)
            except Exception as exc:
                logger.exception("tool_after 钩子执行失败: %s", exc)

    async def turn_end(self, ctx: TurnContext) -> None:
        for _, _, hook in self._ordered():
            try:
                await hook.turn_end(ctx)
            except Exception as exc:
                logger.exception("turn_end 钩子执行失败: %s", exc)
