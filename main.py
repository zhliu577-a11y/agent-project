# main.py —— Harness 启动器：发现插件 → 装配网关 → 进入固定 agent loop（全异步）
#
# 启动流程：
#   1. assemble_plugins 统一装配 plugins/（hook / mcp / tool / model / skill / session）；
#   2. 钩子插件装进 HookGateway，本地工具直接注册进 ToolRegistry；
#   3. 按 AGENT_MODEL（默认 deepseek）选定模型插件并惰性实例化；
#   4. 创建 SkillGateway / McpGateway，注册 use_skill / use_plugin，进入固定 loop。
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from core.config import AppConfig
from core.context import trim_history
from core.events import Event, EventBus, jsonl_sink
from core.hooks import HookGateway
from core.prompt import build_system_prompt
from core.registry import ToolRegistry
from core.state import capture
from core.tracing import begin_trace, setup_logging
from core.types import Message
from gateways.mcp_gateway import McpGateway, UsePlugin
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from gateways.session_gateway import SessionGateway
from gateways.skill_gateway import SkillGateway, UseSkill
from loop import run_agent
from plugins.loader import assemble_plugins, attach_listener_plugins

logger = logging.getLogger("main")


async def chat(
    model,
    tools,
    hooks,
    system_prompt: str,
    history: list[Message] | None = None,
    session: SessionGateway | None = None,
    max_context_tokens: int = 20000,
    events: EventBus | None = None,
) -> list[Message]:
    """交互循环；返回本会话最终历史（不含 system），供持久化/恢复。"""
    logger.info("对话已启动，输入 exit / quit / 退出 结束。")
    history = list(history or [])
    session_id = session.session_id if session is not None else None

    async def _publish(name: str, **payload: object) -> None:
        if events is None:
            return
        await events.publish(Event(name=name, payload=dict(payload)))

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
        if events is not None:
            decision = await events.decide(
                Event("user_prompt.submit", {"session_id": session_id, "text": user_input})
            )
            if decision != "allow":
                logger.warning("用户输入被事件总线策略拦截: %s", decision)
                print(f"[bus] 本轮输入被策略拦截({decision})")
                continue
        history, dropped = trim_history(history, max_context_tokens)
        if dropped:
            logger.warning(
                "上下文超出预算，已裁剪 %d 条最早消息（剩余 %d 条）",
                dropped,
                len(history),
            )
        ctx = await run_agent(
            model,
            tools,
            hooks,
            system_prompt,
            user_input,
            on_token=on_token,
            history=history,
            events=events,
        )
        history = ctx.messages[1:]  # 去掉 system，其余全部进入下一轮上下文
        if session is not None:
            await session.save_history(history)
            await session.save_checkpoint(capture(ctx))

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
    plugins_dir = Path(__file__).resolve().parent / "plugins"

    # 0. 装配插件：写错清单/入口立刻报错退出，而不是静默出错
    try:
        assembly = assemble_plugins(plugins_dir)
    except ValueError as exc:
        logger.error("插件装配失败: %s", exc)
        return
    hook_plugins = assembly.hooks
    mcp_specs = assembly.mcp
    tool_plugins = assembly.tools
    model_plugins = assembly.models

    # 1. 钩子网关：内核只面向它，具体钩子全部来自插件目录
    hooks = HookGateway()
    for manifest, hook in hook_plugins:
        hooks.add(hook)
        logger.info("钩子网关已接入插件: %s", manifest.name)

    # 1.5 事件总线：观察层（hook 网关桥接其上；EVENT_LOG 可落 JSONL 时间线）
    bus = EventBus()
    event_log = os.getenv("EVENT_LOG")
    if event_log:
        bus.subscribe("*", jsonl_sink(event_log))
        logger.info("事件总线日志已启用: %s", event_log)
    hooks.attach(bus)
    attach_listener_plugins(bus, assembly.listeners)
    if assembly.listeners:
        logger.info("listener 插件已接入: %s", [p.manifest.name for p in assembly.listeners])

    # 2.5 模型插件：AGENT_MODEL 选择（默认 deepseek），选定后才实例化
    model_name = config.model
    model_plugin = next(
        (plugin for plugin in model_plugins if plugin.manifest.name == model_name), None
    )
    if model_plugin is None:
        logger.error(
            "未知的模型插件: %s，可选: %s", model_name, [p.manifest.name for p in model_plugins]
        )
        return
    try:
        model = model_plugin.create()
    except ValueError as exc:
        logger.error("模型插件 %s 初始化失败: %s", model_name, exc)
        return

    # 2.6 会话存储：SESSION_STORE 选择（默认 jsonl），按 SESSION_ID 恢复历史
    session_store_name = config.session_store
    session_plugin = next(
        (plugin for plugin in assembly.sessions if plugin.manifest.name == session_store_name),
        None,
    )
    if session_plugin is None:
        logger.error(
            "未知的会话存储插件: %s，可选: %s",
            session_store_name,
            [p.manifest.name for p in assembly.sessions],
        )
        return
    try:
        store = session_plugin.create()
    except ValueError as exc:
        logger.error("会话存储插件 %s 初始化失败: %s", session_store_name, exc)
        return
    session = SessionGateway(store, session_id=config.session_id)
    try:
        history = await session.load_history()
    except (OSError, ValueError) as exc:
        logger.warning("会话历史读取失败，将开启新会话: %s", exc)
        history = []
    logger.info(
        "会话 %s（%s 存储）恢复历史 %d 条",
        session.session_id,
        session_store_name,
        len(history),
    )

    # 2.7 长期记忆：MEMORY_STORE 选择（默认 jsonl），跨会话语义笔记
    memory_store_name = config.memory_store
    memory_plugin = next(
        (plugin for plugin in assembly.memories if plugin.manifest.name == memory_store_name),
        None,
    )
    if memory_plugin is None:
        logger.error(
            "未知的长期记忆插件: %s，可选: %s",
            memory_store_name,
            [p.manifest.name for p in assembly.memories],
        )
        return
    try:
        memory_store = memory_plugin.create()
    except ValueError as exc:
        logger.error("长期记忆插件 %s 初始化失败: %s", memory_store_name, exc)
        return
    memory_gateway = MemoryGateway(memory_store, events=bus)
    logger.info("长期记忆已启用（%s 存储）", memory_store_name)

    tools = ToolRegistry()

    # 3. 本地工具直接进注册表（启动即就绪）
    for _, tools_list in tool_plugins:
        for tool in tools_list:
            tools.register(tool)
        logger.info("本地工具已就绪: %s", [tool.name for tool in tools_list])

    # 4. 技能网关：提示词只放目录，正文由 use_skill 按需读取（渐进披露）
    skill_gateway = SkillGateway(assembly.skills)
    tools.register(UseSkill(skill_gateway))
    logger.info("技能目录: %s", skill_gateway.available())

    # 4.5 长期记忆工具：remember / recall / forget（跨会话笔记）
    tools.register(RememberTool(memory_gateway))
    tools.register(RecallTool(memory_gateway))
    tools.register(ForgetTool(memory_gateway))
    tools.register(UpdateNoteTool(memory_gateway))
    logger.info("长期记忆工具已就绪: remember / recall / forget / update_note")

    # 5. 组装系统提示词：只放目录条目；本地工具说明在其 schema 里，不重复写
    preload_parts: list[tuple[str, str]] = []
    for skill in assembly.skills:
        if not skill.preload:
            continue
        try:
            content = skill_gateway.get(skill.manifest.name)
        except ValueError as exc:
            logger.warning("预载技能 %s 读取失败，已跳过: %s", skill.manifest.name, exc)
            continue
        preload_parts.append((skill.manifest.name, content))
    system_prompt = build_system_prompt(
        mcp=[(spec.manifest.name, spec.manifest.description) for spec in mcp_specs],
        skills=skill_gateway.catalog(),
        preloads=preload_parts,
    )

    logger.info("MCP 插件目录（尚未连接）: %s", [spec.manifest.name for spec in mcp_specs])

    # 6. MCP 网关：整个应用生命周期里唯一持有 MCP 连接的对象
    gateway = McpGateway(mcp_specs)
    try:
        # 只注册"挂载器"工具，具体 MCP 插件等模型决定后再由网关连接
        tools.register(UsePlugin(gateway, tools))
        await chat(
            model,
            tools,
            hooks,
            system_prompt,
            history=history,
            session=session,
            max_context_tokens=config.context_max_tokens,
            events=bus,
        )
    finally:
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
