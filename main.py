# main.py —— 入口（全异步）：DeepSeek + 插件目录按需加载 MCP + 权限钩子 + 流式输出
import asyncio
import logging
import sys

from dotenv import load_dotenv

from core.hooks import HookManager
from core.registry import ToolRegistry
from loop import run_agent
from mcp_bridge import McpBridge
from models.openai_compat import OpenAICompatModel
from permission import load_policy
from plugin_loader import UseServer, load_directory

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

    # 1. 只读插件目录，不连接任何服务器
    entries = load_directory("mcp_servers.json")
    catalog = "\n".join(f"- {e.name}: {e.description}" for e in entries)
    system_prompt = (
        "你是一个乐于助人的助手。你可以通过调用 use_server 按需加载工具服务器。\n"
        f"可用服务器：\n{catalog}\n"
        "需要用到某台服务器时，先调用 use_server 加载它，加载成功后再使用它提供的工具。"
    )
    logger.info("插件目录（尚未连接）: %s", [e.name for e in entries])

    model = OpenAICompatModel()
    tools = ToolRegistry()

    # 2. 权限钩子：按 permission.json 策略放行/询问/拦截工具调用
    hooks = HookManager()
    hooks.add(load_policy("permission.json"))

    bridge = McpBridge()
    try:
        # 3. 只注册"加载器"工具，具体服务器等模型决定后再连
        tools.register(UseServer(bridge, tools, entries, python=sys.executable))
        await chat(model, tools, hooks, system_prompt)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
