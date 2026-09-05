# main.py —— Harness 启动器：发现插件 → 装配网关 → 进入固定 agent loop（全异步）
#
# 启动流程：
#   1. 扫描 plugins/ 目录，加载全部钩子插件（hooks/*）与 MCP 插件清单（mcp/*）；
#   2. 把所有钩子插件装进 HookGateway（钩子网关）；
#   3. 创建 McpGateway（MCP 网关：内核与 MCP 世界之间的唯一通道）；
#   4. 注册 use_plugin 挂载工具，进入固定 loop；模型按需让网关挂载插件。
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv

from core.hooks import HookGateway
from core.registry import ToolRegistry
from gateways.mcp_gateway import McpGateway, UsePlugin
from loop import run_agent
from models.openai_compat import OpenAICompatModel
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

    # 1. 钩子网关：内核只面向它，具体钩子全部来自插件目录
    hooks = HookGateway()
    for manifest, hook in hook_plugins:
        hooks.add(hook)
        logger.info("钩子网关已接入插件: %s", manifest.name)

    # 2. 本地工具启动即注册；MCP 插件只读清单，模型需要时由网关挂载
    local_catalog = (
        "\n".join(
            f"- {tool.name}: {tool.description}" for _, tools in tool_plugins for tool in tools
        )
        or "（暂无）"
    )
    mcp_catalog = (
        "\n".join(f"- {spec.manifest.name}: {spec.manifest.description}" for spec in mcp_specs)
        or "（暂无）"
    )
    system_prompt = (
        "你是一个乐于助人的助手。工具名格式为 <插件名>__<工具名>，分两类：\n"
        "1. 本地工具（进程内实现，启动即就绪，可直接调用）：\n"
        f"{local_catalog}\n"
        "2. MCP 插件工具：需要某个 MCP 插件时，先调用 use_plugin 挂载它，"
        "挂载成功后再调用其工具。\n"
        f"可挂载的 MCP 插件：\n{mcp_catalog}"
    )
    logger.info("MCP 插件目录（尚未连接）: %s", [spec.manifest.name for spec in mcp_specs])

    model = OpenAICompatModel()
    tools = ToolRegistry()

    # 3. 本地工具直接进注册表；MCP 网关是整个应用生命周期里唯一持有 MCP 连接的对象
    for _, tools_list in tool_plugins:
        for tool in tools_list:
            tools.register(tool)
        logger.info("本地工具已就绪: %s", [tool.name for tool in tools_list])

    gateway = McpGateway(mcp_specs)
    try:
        # 4. 只注册"挂载器"工具，具体 MCP 插件等模型决定后再由网关连接
        tools.register(UsePlugin(gateway, tools))
        await chat(model, tools, hooks, system_prompt)
    finally:
        await gateway.close()


if __name__ == "__main__":
    asyncio.run(main())
