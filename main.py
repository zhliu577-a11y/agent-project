# main.py —— 入口：用 DeepSeek 跑通固定 loop
import logging

from dotenv import load_dotenv

from core.hooks import HookManager
from core.registry import ToolRegistry
from loop import run_agent
from models.openai_compat import OpenAICompatModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    load_dotenv()

    model = OpenAICompatModel()
    tools = ToolRegistry()
    hooks = HookManager()

    print("已连接 DeepSeek。输入 exit / quit / 退出 结束对话。")
    while True:
        user_input = input("你: ").strip()
        if user_input.lower() in {"exit", "quit", "退出"}:
            break

        ctx = run_agent(model, tools, hooks, "你是一个乐于助人的助手。", user_input)
        print(f"助手: {ctx.messages[-1].content}")
        if ctx.stop_reason != "done":
            print(f"[提示] 本轮结束原因: {ctx.stop_reason}")


if __name__ == "__main__":
    main()