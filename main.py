# main.py —— Harness 启动器：发现插件 → 装配网关 → 进入固定 agent loop（全异步）
#
# 启动流程：
#   1. assemble_plugins 统一装配 plugins/（hook / mcp / tool / model / skill）；
#   2. 钩子插件装进 HookGateway，本地工具直接注册进 ToolRegistry；
#   3. 按 AGENT_MODEL（默认 deepseek）选定模型插件并惰性实例化；
#   4. 创建 SkillGateway / McpGateway，注册 use_skill / use_plugin，进入固定 loop。
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from core.hooks import HookGateway
from core.registry import ToolRegistry
from gateways.mcp_gateway import McpGateway, UsePlugin
from gateways.skill_gateway import SkillGateway, UseSkill
from loop import run_agent
from plugins.loader import assemble_plugins

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def chat(model, tools, hooks, system_prompt: str) -> None:
    logger.info("对话已启动，输入 exit / quit / 退出 结束。")

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
        ctx = await run_agent(model, tools, hooks, system_prompt, user_input, on_token=on_token)

        if not streamed["active"]:
            # 没有流式输出（例如被拒绝或出错），整段补打
            print(f"助手: {ctx.messages[-1].content}")
        else:
            print()  # 流式输出已结束，补一个换行
        if ctx.stop_reason != "done":
            logger.warning("本轮结束原因: %s", ctx.stop_reason)


async def main() -> None:
    load_dotenv()
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

    # 2.5 模型插件：AGENT_MODEL 选择（默认 deepseek），选定后才实例化
    model_name = os.getenv("AGENT_MODEL", "deepseek")
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

    # 5. 组装系统提示词：只放目录条目；本地工具说明在其 schema 里，不重复写
    mcp_catalog = (
        "\n".join(f"- {spec.manifest.name}: {spec.manifest.description}" for spec in mcp_specs)
        or "（暂无）"
    )
    skill_catalog = "\n".join(f"- {name}: {desc}" for name, desc in skill_gateway.catalog())
    preload_parts: list[str] = []
    for skill in assembly.skills:
        if not skill.preload:
            continue
        try:
            content = skill_gateway.get(skill.manifest.name)
        except ValueError as exc:
            logger.warning("预载技能 %s 读取失败，已跳过: %s", skill.manifest.name, exc)
            continue
        preload_parts.append(f"### 技能 {skill.manifest.name}\n{content}")
    system_prompt = (
        "你是一个乐于助人的助手。工具名格式为 <插件名>__<工具名>；"
        "本地工具启动即就绪，说明见各自的函数 schema。\n"
        "需要某个 MCP 插件时，先调用 use_plugin 挂载它，挂载成功后再调用其工具。\n"
        f"可挂载的 MCP 插件：\n{mcp_catalog}\n"
        "需要某项技能时，先调用 use_skill 读取完整说明，再按其执行。\n"
        f"可用技能：\n{skill_catalog or '（暂无）'}"
    )
    if preload_parts:
        system_prompt += f"\n\n以下为预载技能（全局规则，必须遵守）：\n{'\n\n'.join(preload_parts)}"

    logger.info("MCP 插件目录（尚未连接）: %s", [spec.manifest.name for spec in mcp_specs])

    # 6. MCP 网关：整个应用生命周期里唯一持有 MCP 连接的对象
    gateway = McpGateway(mcp_specs)
    try:
        # 只注册"挂载器"工具，具体 MCP 插件等模型决定后再由网关连接
        tools.register(UsePlugin(gateway, tools))
        await chat(model, tools, hooks, system_prompt)
    finally:
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
