# loop.py —— 内核：固定 agent loop（异步）
import logging

from core.hooks import HookManager
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.types import Message, ModelResponse, TurnContext

logger = logging.getLogger(__name__)


async def run_agent(
    model: ModelAdapter,
    tools: ToolRegistry,
    hooks: HookManager,
    system_prompt: str,
    user_input: str,
    max_turns: int = 20,
) -> TurnContext:
    """执行固定循环：调模型 → 执行工具 → 回填 → 直到模型不再请求工具。"""
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
            resp: ModelResponse = await model.complete(ctx.messages, tools.list_schemas())
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

        for tc in resp.tool_calls:
            await hooks.tool_before(ctx, tc)
            try:
                result = await tools.execute(tc.name, tc.arguments)
                ok = True
            except Exception as exc:
                logger.exception("工具 %s 执行失败: %s", tc.name, exc)
                result, ok = f"工具执行失败: {exc}", False
            await hooks.tool_after(ctx, tc, result, ok)
            ctx.messages.append(
                Message(role="tool", content=str(result), tool_call_id=tc.id)
            )

        ctx.turn += 1
        await hooks.turn_end(ctx)

    return ctx
