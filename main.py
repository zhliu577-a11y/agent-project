# main.py —— 入口：用 DeepSeek 跑通固定 loop + MCP 工具
import logging
import sys

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


def chat(model, tools, hooks) -> None:
    print("已连接 DeepSeek 与 MCP 工具。输入 exit / quit / 退出 结束对话。")
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() in {"exit", "quit", "退出"}:
            break

        ctx = run_agent(model, tools, hooks, "你是一个乐于助人的助手。", user_input)
        print(f"助手: {ctx.messages[-1].content}")
        if ctx.stop_reason != "done":
            print(f"[提示] 本轮结束原因: {ctx.stop_reason}")


def main() -> None:
    load_dotenv()

    model = OpenAICompatModel()
    tools = ToolRegistry()
    hooks = HookManager()

    bridge = McpBridge()
    try:
        names = register_mcp_server(
            tools, bridge,
            command=sys.executable,
            args=["mcp_servers/time_server.py"],
        )
        print(f"已注册 MCP 工具: {names or '（无）'}")
        chat(model, tools, hooks)
    finally:
        bridge.close()


if __name__ == "__main__":
    main()