# loop.py —— 内核：固定 agent loop（异步，支持流式、并行与失败自纠）
import asyncio
import logging
from collections.abc import Callable

from core.context import (
    ContextPolicy,
    ContextRequest,
    ContextResult,
    TailWindowPolicy,
    request_tokens,
    valid_tool_call_sequence,
)
from core.errors import RetryableError, classify_error
from core.events import Event, EventBus
from core.hooks import ConfirmFn, HookGateway
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tracing import current_trace_id
from core.types import Message, ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)

# 同一工具连续失败达到该次数后，自动禁用，防止模型死循环重试
FAIL_LIMIT = 3
_DEFAULT_CONTEXT_POLICY = TailWindowPolicy()


async def run_agent(
    model: ModelAdapter,
    tools: ToolRegistry,
    hooks: HookGateway,
    system_prompt: str,
    user_input: str,
    max_turns: int = 20,
    on_token: Callable[[str], None] | None = None,
    history: list[Message] | None = None,
    confirm: ConfirmFn | None = None,
    events: EventBus | None = None,
    context_policy: ContextPolicy | None = None,
    max_context_tokens: int = 20000,
    session_id: str | None = None,
) -> TurnContext:
    """执行固定循环：调模型 → 执行工具 → 回填 → 直到模型不再请求工具。

    history：本会话此前的消息（不含 system），用于多轮上下文延续。
    """
    ctx = TurnContext(
        messages=[Message(role="system", content=system_prompt)],
        max_turns=max_turns,
    )
    if history:
        ctx.messages.extend(history)
    ctx.messages.append(Message(role="user", content=user_input))
    ctx.state.setdefault("fail_counts", {})  # 工具名 -> 连续失败次数
    ctx.state.setdefault("blocked_tools", set())  # 已禁用的工具名集合

    if events is not None:
        hooks.attach(events)  # 旧 hook 插件通过总线桥接，行为不变

    async def _emit(name: str, **payload: object) -> None:
        if events is None:
            return
        await events.publish(Event(name=name, payload=dict(payload), trace_id=current_trace_id()))

    async def _end_turn() -> None:
        if events is None:
            await hooks.turn_end(ctx)
        else:
            await _emit("turn.end", ctx=ctx)

    active_context_policy = context_policy or _DEFAULT_CONTEXT_POLICY

    async def _prepare_model_messages(
        tool_schemas: list[dict],
    ) -> list[Message]:
        request = ContextRequest(
            messages=list(ctx.messages),
            tools=tool_schemas,
            max_tokens=max_context_tokens,
            session_id=session_id,
            turn=ctx.turn,
            state=ctx.state,
        )
        try:
            result = await active_context_policy.prepare(request)
            if not isinstance(result, ContextResult):
                raise TypeError(
                    f"context policy must return ContextResult, got {type(result).__name__}"
                )
            if not result.messages:
                raise ValueError("context policy returned no messages")
            if not all(isinstance(message, Message) for message in result.messages):
                raise TypeError("context policy returned a non-Message value")
            if result.messages[0].role != "system":
                raise ValueError("context policy must preserve system instructions")
            if not valid_tool_call_sequence(result.messages):
                raise ValueError("context policy returned orphaned tool call messages")
            if request_tokens(result.messages, tool_schemas) > max_context_tokens:
                raise ValueError("context policy exceeded the token budget")
            return result.messages
        except Exception as exc:
            logger.warning(
                "上下文策略 %s 失败，回退到 tail-window: %s",
                type(active_context_policy).__name__,
                exc,
            )

        fallback = await _DEFAULT_CONTEXT_POLICY.prepare(request)
        if request_tokens(fallback.messages, tool_schemas) > max_context_tokens:
            logger.warning("tail-window 仍无法容纳当前请求；将保留最新消息并交由模型处理")
        return fallback.messages

    while ctx.turn < ctx.max_turns:
        if events is None:
            await hooks.turn_start(ctx)
        else:
            await _emit("turn.start", ctx=ctx)

        try:
            schemas = tools.list_schemas()
            model_messages = await _prepare_model_messages(schemas)
            await _emit("model.request", ctx=ctx, tools=len(schemas))
            resp: ModelResponse = await model.complete(
                model_messages,
                schemas,
                on_token=on_token,
            )
        except Exception as exc:
            error = classify_error(exc)
            category = getattr(error, "category", type(error).__name__)
            logger.exception("模型调用失败（%s）: %s", category, error)
            ctx.state["last_error"] = {"category": category, "message": str(error)}
            await _emit("model.error", ctx=ctx, category=category, message=str(error))
            ctx.stop_reason = "error"
            await _end_turn()
            break

        if events is None:
            await hooks.llm_response(ctx, resp)
        else:
            await _emit("model.response", ctx=ctx, resp=resp)

        if not resp.tool_calls:
            ctx.stop_reason = "done"
            ctx.messages.append(Message(role="assistant", content=resp.content))
            await _end_turn()
            break

        ctx.messages.append(
            Message(role="assistant", content=resp.content, tool_calls=resp.tool_calls)
        )

        # 1) 权限检查：可能包含用户询问，逐个按顺序执行
        approved: list[ToolCall] = []
        for tc in resp.tool_calls:
            allowed = await hooks.tool_before(ctx, tc, confirm=confirm)
            if not allowed:
                reason = "工具调用被权限策略拒绝"
                if events is None:
                    await hooks.tool_after(ctx, tc, reason, False)
                else:
                    await _emit("tool.denied", ctx=ctx, tool_call=tc)
                    await _emit("tool.after", ctx=ctx, tool_call=tc, result=reason, ok=False)
                ctx.messages.append(Message(role="tool", content=reason, tool_call_id=tc.id))
                continue
            approved.append(tc)

        # 2) 并行执行被放行的工具（asyncio.gather 保持返回顺序）
        async def _execute_one(tc: ToolCall) -> tuple[ToolCall, object, bool]:
            await _emit("tool.start", ctx=ctx, tool_call=tc)
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
                    error = classify_error(exc)
                    category = getattr(error, "category", type(error).__name__)
                    retryable = isinstance(error, RetryableError) or (
                        getattr(error, "category", None) == "retryable"
                    )
                    retry_hint = "（瞬时错误，可稍后重试）" if retryable else ""
                    code = getattr(error, "code", None)
                    hint = getattr(error, "hint", "")
                    code_part = f"，code: {code}" if code else ""
                    hint_part = f"提示: {hint}。" if hint else ""
                    desc = tools.describe(tc.name)
                    schema_hint = ""
                    if desc is not None:
                        schema_hint = f"；期望参数 schema: {desc['parameters']}"
                    message = (
                        f"工具 {tc.name} 执行失败: {exc}{schema_hint}。"
                        f"错误类别: {category}{code_part}{retry_hint}。"
                        f"{hint_part}"
                        "如果这是参数问题，请修正参数后重试；否则请换一种方法。"
                    )
                return tc, message, False

        results = await asyncio.gather(*(_execute_one(tc) for tc in approved))

        # 3) 按原顺序回填结果
        for tc, result, ok in results:
            if events is None:
                await hooks.tool_after(ctx, tc, result, ok)
            else:
                await _emit("tool.after", ctx=ctx, tool_call=tc, result=result, ok=ok)
            ctx.messages.append(Message(role="tool", content=str(result), tool_call_id=tc.id))

        ctx.turn += 1
        await _end_turn()

    return ctx
