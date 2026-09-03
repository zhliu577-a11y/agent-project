# main.py —— 入口（全异步）：DeepSeek + MCP 工具
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from core.hooks import HookManager
from core.registry import ToolRegistry
from loop import run_agent
from mcp_bridge import McpBridge, register_mcp_server
from models.openai_compat import OpenAICompatModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


async def chat(model, tools, hooks) -> None:
    print("已连接 DeepSeek 与 MCP 工具。输入 exit / quit / 退出 结束对话。")
    while True:
        user_input = input("你: ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "退出"}:
            break

        ctx = await run_agent(model, tools, hooks, "你是一个乐于助人的助手。", user_input)
        print(f"助手: {ctx.messages[-1].content}")
        if ctx.stop_reason != "done":
            print(f"[提示] 本轮结束原因: {ctx.stop_reason}")


async def main() -> None:
    load_dotenv()

    model = OpenAICompatModel()
    tools = ToolRegistry()
    hooks = HookManager()

    bridge = McpBridge()
    try:
        # 配置驱动：连哪些 MCP 服务器由 mcp_servers.json 决定，不改代码
        config = json.loads(Path("mcp_servers.json").read_text(encoding="utf-8"))
        names: list[str] = []
        for server in config["servers"]:
            command = server["command"]
            if command == "python":
                command = sys.executable  # 用当前解释器启动服务器子进程
            names += await register_mcp_server(
                tools, bridge,
                command=command,
                args=server["args"],
            )
        print(f"已注册 MCP 工具: {names}")
        await chat(model, tools, hooks)
    finally:
        await bridge.close()


if __name__ == "__main__":
    asyncio.run(main())
