# loop.py —— 内核：固定 agent loop（异步，支持流式输出与并行工具）
import asyncio
import logging
from collections.abc import Callable

from core.hooks import HookManager
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import Message, ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)


async def run_agent(
    model: ModelAdapter,
    tools: ToolRegistry,
    hooks: HookManager,
    system_prompt: str,
    user_input: str,
    max_turns: int = 20,
    on_token: Callable[[str], None] | None = None,
) -> TurnContext:
    """执行固定循环：调模型 → 执行工具 → 回填 → 直到模型不再请求工具。

    on_token：透传给模型适配器，用于流式显示最终回答。
    """
    ctx = TurnContext(
        messages=[
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_input),
        ],
        max_turns=max_turns,
    )

    while ctx.turn < ctx.max_turns:
        await hooks.turn_start(ctx)

        try:
            resp: ModelResponse = await model.complete(
                ctx.messages, tools.list_schemas(), on_token=on_token
            )
        except Exception as exc:
            logger.exception("模型调用失败: %s", exc)
            ctx.stop_reason = "error"
            break

        await hooks.llm_response(ctx, resp)

        if not resp.tool_calls:
            ctx.stop_reason = "done"
            ctx.messages.append(Message(role="assistant", content=resp.content))
            break

        ctx.messages.append(
            Message(role="assistant", content=resp.content, tool_calls=resp.tool_calls)
        )

        # 1) 权限检查：可能包含用户询问，逐个按顺序执行
        approved: list[ToolCall] = []
        for tc in resp.tool_calls:
            allowed = await hooks.tool_before(ctx, tc)
            if not allowed:
                reason = "工具调用被权限策略拒绝"
                await hooks.tool_after(ctx, tc, reason, False)
                ctx.messages.append(
                    Message(role="tool", content=reason, tool_call_id=tc.id)
                )
                continue
            approved.append(tc)

        # 2) 并行执行被放行的工具（asyncio.gather 保持返回顺序）
        async def _execute_one(tc: ToolCall) -> tuple[ToolCall, object, bool]:
            try:
                result = await tools.execute(tc.name, tc.arguments)
                ok = True
            except Exception as exc:
                logger.exception("工具 %s 执行失败: %s", tc.name, exc)
                result, ok = f"工具执行失败: {exc}", False
            return tc, result, ok

        results = await asyncio.gather(*(_execute_one(tc) for tc in approved))

        # 3) 按原顺序回填结果
        for tc, result, ok in results:
            await hooks.tool_after(ctx, tc, result, ok)
            ctx.messages.append(
                Message(role="tool", content=str(result), tool_call_id=tc.id)
            )

        ctx.turn += 1
        await hooks.turn_end(ctx)

    return ctx
