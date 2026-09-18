# plugins/model/sub2api/model.py - Sub2API via the OpenAI Responses protocol
import json
import os
from collections.abc import Callable, Sequence
from typing import Any

from openai import AsyncOpenAI, DefaultAsyncHttpx2Client

from core.model import ModelAdapter
from core.parser import OpenAIResponsesParser
from core.types import Message, ModelResponse
from plugins.context import PluginContext


def messages_to_responses_input(
    messages: list[Message],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert internal messages to Responses API instructions and input items."""
    instructions: list[str] = []
    items: list[dict[str, Any]] = []

    for message in messages:
        if message.role == "system":
            if message.content:
                instructions.append(message.content)
            continue

        if message.role == "assistant":
            if message.content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": message.content}],
                    }
                )
            for tool_call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                    }
                )
            continue

        if message.role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                }
            )
            continue

        items.append({"role": message.role, "content": message.content})

    return "\n\n".join(instructions) or None, items


def tool_schemas_to_responses(
    tool_schemas: Sequence[dict[str, object]],
) -> list[dict[str, Any]]:
    """Flatten Chat Completions function schemas into Responses tool schemas."""
    tools: list[dict[str, Any]] = []
    for schema in tool_schemas:
        function = schema.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        tool: dict[str, Any] = {
            "type": "function",
            "name": name,
            "parameters": function.get("parameters", {"type": "object", "properties": {}}),
        }
        description = function.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        tools.append(tool)
    return tools


class Sub2APIModel(ModelAdapter):
    """Call a Responses-compatible Sub2API gateway."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        model: str,
        timeout: float,
        max_retries: int,
        disable_response_storage: bool,
    ) -> None:
        http_client = DefaultAsyncHttpx2Client(
            trust_env=False,
            timeout=timeout,
        )
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._model = model
        self._disable_response_storage = disable_response_storage

    async def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
        on_token: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        instructions, input_items = messages_to_responses_input(messages)
        payload: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "stream": True,
            "store": not self._disable_response_storage,
        }
        if instructions is not None:
            payload["instructions"] = instructions
        tools = tool_schemas_to_responses(tool_schemas)
        if tools:
            payload["tools"] = tools

        stream = await self._client.responses.create(**payload)
        parser = OpenAIResponsesParser(on_token=on_token)
        async for event in stream:
            parser.feed(event)
        return parser.finalize()

    async def stop(self) -> None:
        await self._client.close()


def create_model(plugin_dir, context: PluginContext | None = None):
    """Create the provider from plugin config first, environment variables second."""
    del plugin_dir
    config = dict(context.config) if context is not None else {}
    api_key = config.get("apiKey") or os.getenv("SUB2API_API_KEY")
    if not api_key:
        raise RuntimeError("missing SUB2API_API_KEY; configure it in .env")

    return Sub2APIModel(
        api_key=str(api_key),
        base_url=str(
            config.get("baseUrl") or os.getenv("SUB2API_BASE_URL", "http://172.16.3.6:8589/v1")
        ),
        model=str(config.get("model") or os.getenv("SUB2API_MODEL", "deepseek-v4-flash")),
        timeout=float(config.get("timeout", os.getenv("SUB2API_TIMEOUT", "60"))),
        max_retries=int(config.get("maxRetries", os.getenv("SUB2API_MAX_RETRIES", "3"))),
        disable_response_storage=_as_bool(
            config.get(
                "disableResponseStorage",
                os.getenv("SUB2API_DISABLE_RESPONSE_STORAGE", "true"),
            )
        ),
    )


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"invalid boolean value: {value!r}")
