# plugins/model/deepseek/model.py —— 模型插件：DeepSeek（OpenAI 兼容协议）
#
# 一个模型插件 = 实现 core.model.ModelAdapter 的类 + 一个工厂函数。
# 工厂接收插件目录（Path），返回 ModelAdapter 实例；实例化会读取
# DEEPSEEK_* 环境变量，因此由 Harness 在选定激活插件后才调用（惰性初始化）。
import json
import os
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from core.model import ModelAdapter
from core.types import Message, ModelResponse, ToolCall


def message_to_payload(msg: Message) -> dict[str, Any]:
    """把内核 Message 转成 OpenAI 兼容 API 的消息格式。"""
    payload: dict[str, Any] = {"role": msg.role, "content": msg.content}

    if msg.role == "assistant" and msg.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in msg.tool_calls
        ]

    if msg.role == "tool":
        payload["tool_call_id"] = msg.tool_call_id

    return payload


class OpenAICompatModel(ModelAdapter):
    """通过 OpenAI 兼容接口异步调用 DeepSeek（也适用于 OpenAI、通义、本地 vLLM）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置后再运行")

        # 超时与重试：客户端内置指数退避，仅重试连接错误/429/5xx 等安全场景
        timeout = float(os.getenv("DEEPSEEK_TIMEOUT", "60"))
        max_retries = int(os.getenv("DEEPSEEK_MAX_RETRIES", "3"))

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=timeout,
            max_retries=max_retries,
        )
        self._model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    async def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
        on_token: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message_to_payload(m) for m in messages],
        }
        if tool_schemas:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "auto"

        # 全程流式：文本增量即时回调；工具调用增量静默累积
        stream = await self._client.chat.completions.create(**payload, stream=True)

        content_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, str]] = {}
        saw_tool_call = False

        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.tool_calls:
                saw_tool_call = True
                for tc in delta.tool_calls:
                    acc = tool_calls_acc.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["arguments"] += tc.function.arguments

            if delta.content:
                content_parts.append(delta.content)
                if on_token is not None and not saw_tool_call:
                    on_token(delta.content)

        tool_calls = [
            ToolCall(
                id=acc["id"] or f"call_{index}",
                name=acc["name"],
                arguments=json.loads(acc["arguments"] or "{}"),
            )
            for index, acc in sorted(tool_calls_acc.items())
        ]
        return ModelResponse(content="".join(content_parts), tool_calls=tool_calls)


def create_model(plugin_dir):
    """插件工厂：返回模型适配器实例（实例化时读取 DEEPSEEK_* 环境变量）。"""
    return OpenAICompatModel()
