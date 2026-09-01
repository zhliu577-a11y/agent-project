# models/openai_compat.py —— OpenAI 兼容模型适配器（DeepSeek / OpenAI / 本地 vLLM）
import json
import os
from typing import Any

from openai import OpenAI

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
    """通过 OpenAI 兼容接口调用 DeepSeek（也适用于 OpenAI、通义、本地 vLLM）。"""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置后再运行")

        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
        self._model = model or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [message_to_payload(m) for m in messages],
        }
        if tool_schemas:
            payload["tools"] = tool_schemas
            payload["tool_choice"] = "auto"

        resp = self._client.chat.completions.create(**payload)
        choice = resp.choices[0].message

        tool_calls = [
            ToolCall(
                id=c.id,
                name=c.function.name,
                arguments=json.loads(c.function.arguments or "{}"),
            )
            for c in (choice.tool_calls or [])
        ]
        return ModelResponse(content=choice.content or "", tool_calls=tool_calls, raw=resp)