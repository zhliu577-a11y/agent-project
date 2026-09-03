# loop.py —— 内核：固定 agent loop（异步，支持流式、并行与失败自纠）
import asyncio
import logging
from collections.abc import Callable

from core.hooks import HookManager
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import Message, ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)

# 同一工具连续失败达到该次数后，自动禁用，防止模型死循环重试
FAIL_LIMIT = 3


async def run_agent(
    model: ModelAdapter,
    tools: ToolRegistry,
    hooks: HookManager,
    system_prompt: str,
    user_input: str,
    max_turns: int = 20,
    on_token: Callable[[str], None] | None = None,
) -> TurnContext:
    """执行固定循环：调模型 → 执行工具 → 回填 → 直到模型不再请求工具。"""
    ctx = TurnContext(
        messages=[
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_input),
        ],
        max_turns=max_turns,
    )
    ctx.state.setdefault("fail_counts", {})  # 工具名 -> 连续失败次数
    ctx.state.setdefault("blocked_tools", set())  # 已禁用的工具名集合

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
                ctx.messages.append(Message(role="tool", content=reason, tool_call_id=tc.id))
                continue
            approved.append(tc)

        # 2) 并行执行被放行的工具（asyncio.gather 保持返回顺序）
        async def _execute_one(tc: ToolCall) -> tuple[ToolCall, object, bool]:
            # 已禁用的工具：不执行，直接告知模型换方法
            if tc.name in ctx.state["blocked_tools"]:
                return tc, f"工具 {tc.name} 已因连续失败被禁用，请改用其他方法。", False

            try:
                result = await tools.execute(tc.name, tc.arguments)
                ctx.state["fail_counts"].pop(tc.name, None)  # 成功则清零
                return tc, result, True
            except Exception as exc:
                logger.exception("工具 %s 执行失败: %s", tc.name, exc)
                fails = ctx.state["fail_counts"].get(tc.name, 0) + 1
                ctx.state["fail_counts"][tc.name] = fails

                if fails >= FAIL_LIMIT:
                    ctx.state["blocked_tools"].add(tc.name)
                    message = (
                        f"工具 {tc.name} 已连续失败 {FAIL_LIMIT} 次，现已禁用。"
                        "请停止调用它，改用其他工具或直接回答。"
                    )
                else:
                    desc = tools.describe(tc.name)
                    schema_hint = ""
                    if desc is not None:
                        schema_hint = f"；期望参数 schema: {desc['parameters']}"
                    message = (
                        f"工具 {tc.name} 执行失败: {exc}{schema_hint}。"
                        "如果这是参数问题，请修正参数后重试；否则请换一种方法。"
                    )
                return tc, message, False

        results = await asyncio.gather(*(_execute_one(tc) for tc in approved))

        # 3) 按原顺序回填结果
        for tc, result, ok in results:
            await hooks.tool_after(ctx, tc, result, ok)
            ctx.messages.append(Message(role="tool", content=str(result), tool_call_id=tc.id))

        ctx.turn += 1
        await hooks.turn_end(ctx)

    return ctx
