# main.py - CLI 启动层：启动 Runtime 并进入固定 agent loop
import asyncio
import logging
import os

from dotenv import load_dotenv

from config import AppConfig
from core.context import ContextPolicy
from core.events import Event, EventGateway, EventIdentity
from core.hooks import PromptRequest
from core.state import capture
from core.tracing import begin_trace, setup_logging
from core.types import Message
from gateways.memory_extraction_gateway import MemoryExtractionGateway
from gateways.session_gateway import SessionGateway
from gateways.skill_gateway import SkillPermissionDecision
from loop import run_agent
from runtime import HarnessRuntime, RuntimeStartupError

logger = logging.getLogger("main")


async def _approve_skill(
    name: str,
    resource: str | None,
    decision: SkillPermissionDecision,
) -> bool:
    target = f"{name} ({resource})" if resource else name
    rule = decision.matched_pattern or decision.source
    answer = (
        input(f"Skill {target} requires approval (rule {rule!r}). Allow? [y/N] ").strip().lower()
    )
    return answer in {"y", "yes"}


async def chat(
    model,
    tools,
    hooks,
    system_prompt: str,
    history: list[Message] | None = None,
    session: SessionGateway | None = None,
    max_context_tokens: int = 20000,
    events: EventGateway | None = None,
    context_policy: ContextPolicy | None = None,
    memory_extraction: MemoryExtractionGateway | None = None,
) -> list[Message]:
    """交互循环；返回本会话最终历史（不含 system），供持久化/恢复。"""
    logger.info("对话已启动，输入 exit / quit / 退出 结束。")
    history = list(history or [])
    session_id = session.session_id if session is not None else None
    publisher = events.publisher(EventIdentity.host("cli")) if events is not None else None

    async def _publish(name: str, **payload: object) -> None:
        if publisher is None:
            return
        await publisher.publish(
            Event(
                name=name,
                payload=dict(payload),
                session_id=session_id,
            )
        )

    await _publish("session.start", session_id=session_id)

    streamed = {"active": False}

    def on_token(text: str) -> None:
        # 用户可见的对话内容：保持 print（stdout），不走日志
        if not streamed["active"]:
            print("助手: ", end="", flush=True)
            streamed["active"] = True
        print(text, end="", flush=True)

    while True:
        user_input = input("你: ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "退出"}:
            logger.info("用户退出对话")
            break

        streamed["active"] = False
        begin_trace()
        allowed = await hooks.user_prompt_submit(
            PromptRequest(text=user_input, session_id=session_id)
        )
        if not allowed:
            await _publish("user_prompt.rejected", session_id=session_id)
            logger.warning("用户输入被控制面策略拦截")
            print("[policy] 本轮输入未通过策略检查")
            continue
        await _publish("user_prompt.accepted", session_id=session_id)
        previous_ids = {message.id for message in history}
        ctx = await run_agent(
            model,
            tools,
            hooks,
            system_prompt,
            user_input,
            on_token=on_token,
            history=history,
            events=events,
            context_policy=context_policy,
            max_context_tokens=max_context_tokens,
            session_id=session_id,
        )
        history = ctx.messages[1:]
        new_messages = [
            message
            for message in history
            if message.id not in previous_ids and message.metadata.get("compaction") != "summary"
        ]
        if session is not None:
            committed = await session.commit_turn(history, capture(ctx))
            history = list(committed.messages)
            if memory_extraction is not None:
                await memory_extraction.process_turn(committed, new_messages)

        if not streamed["active"]:
            # 没有流式输出（例如被拒绝或出错），整段补打
            print(f"助手: {ctx.messages[-1].content}")
        else:
            print()  # 流式输出已结束，补一个换行
        if ctx.stop_reason != "done":
            logger.warning("本轮结束原因: %s", ctx.stop_reason)
    await _publish("session.end", session_id=session_id)
    return history


async def main() -> None:
    setup_logging(logging.INFO)
    load_dotenv()
    config = AppConfig.load()
    os.environ.setdefault("EMBEDDING_PROVIDER", config.embedding_provider)
    try:
        runtime = HarnessRuntime(config, skill_approver=_approve_skill)
    except (OSError, ValueError) as exc:
        logger.error("Runtime 初始化失败: %s", exc)
        return
    try:
        try:
            await runtime.start()
        except (RuntimeStartupError, OSError, ValueError) as exc:
            logger.error("Runtime 启动失败: %s", exc)
            return
        await chat(
            runtime.model,
            runtime.tools,
            runtime.hooks,
            runtime.system_prompt,
            history=runtime.history,
            session=runtime.session,
            max_context_tokens=config.context_max_tokens,
            events=runtime.events,
            context_policy=runtime.context_gateway or runtime.context_policy,
            memory_extraction=runtime.memory_extraction,
        )
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
