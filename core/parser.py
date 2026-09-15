# core/parser.py —— Response Parser 抽象：把厂商流式分片翻译成 ModelResponse
#
# 定位：内核提供的“协议解析能力”（与 errors/types 同级），不是插件 kind。
# - ResponseParser：插件只写传输，解析委托给本层；
# - ToolCallAccumulator：按 index 合并分片到达的 id/name/arguments；
# - OpenAICompatibleParser：OpenAI 兼容 chat.completions 流的通用实现，
#   deepseek / openai / vLLM 等插件可直接复用；协议不同则另写一个 parser。
import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from core.types import ModelResponse, ToolCall


class ResponseParser(ABC):
    """流式响应解析契约：feed 累积分片，finalize 产出标准结果。"""

    @abstractmethod
    def feed(self, chunk: Any) -> None:
        """喂入一个流式分片。"""
        ...

    @abstractmethod
    def finalize(self) -> ModelResponse:
        """结束流并产出内核统一结构。"""
        ...


@dataclass
class ToolCallAccumulator:
    """按 index 合并工具调用分片（id/name/arguments 都可能分片到达）。"""

    _calls: dict[int, dict[str, str]] = field(default_factory=dict)

    def add(
        self,
        index: int,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: str | None = None,
    ) -> None:
        acc = self._calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call_id:
            acc["id"] = call_id
        if name:
            acc["name"] += name
        if arguments:
            acc["arguments"] += arguments

    def finalize(self) -> list[ToolCall]:
        """按 index 升序产出 ToolCall；arguments 为 JSON 文本，解析失败即报错。"""
        return [
            ToolCall(
                id=acc["id"] or f"call_{index}",
                name=acc["name"],
                arguments=json.loads(acc["arguments"] or "{}"),
            )
            for index, acc in sorted(self._calls.items())
        ]

    def __bool__(self) -> bool:
        return bool(self._calls)


class OpenAICompatibleParser(ResponseParser):
    """OpenAI 兼容流解析：文本增量回调 + 工具调用静默累积。"""

    def __init__(self, on_token: Callable[[str], None] | None = None) -> None:
        self._on_token = on_token
        self._content: list[str] = []
        self._tools = ToolCallAccumulator()
        self._saw_tool_call = False

    def feed(self, chunk: Any) -> None:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            return

        tool_calls = getattr(delta, "tool_calls", None)
        if tool_calls:
            self._saw_tool_call = True
            for call in tool_calls:
                function = getattr(call, "function", None)
                self._tools.add(
                    getattr(call, "index", 0),
                    call_id=getattr(call, "id", None),
                    name=getattr(function, "name", None) if function else None,
                    arguments=getattr(function, "arguments", None) if function else None,
                )

        content = getattr(delta, "content", None)
        if content:
            self._content.append(content)
            # 与旧行为一致：一旦出现工具调用，后续文本不再流式回调（仅累积）
            if self._on_token is not None and not self._saw_tool_call:
                self._on_token(content)

    def finalize(self) -> ModelResponse:
        return ModelResponse(content="".join(self._content), tool_calls=self._tools.finalize())


@dataclass
class _ResponsesToolCall:
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    saw_argument_delta: bool = False


class OpenAIResponsesParser(ResponseParser):
    """Parse OpenAI Responses streaming events into the internal response type."""

    def __init__(self, on_token: Callable[[str], None] | None = None) -> None:
        self._on_token = on_token
        self._content: list[str] = []
        self._tools: dict[int, _ResponsesToolCall] = {}
        self._saw_tool_call = False

    def feed(self, event: Any) -> None:
        event_type = _value(event, "type", "")
        if event_type == "response.output_text.delta":
            delta = _value(event, "delta", "")
            if not isinstance(delta, str) or not delta:
                return
            self._content.append(delta)
            if self._on_token is not None and not self._saw_tool_call:
                self._on_token(delta)
            return

        if event_type == "response.output_item.added":
            self._feed_item(
                _value(event, "output_index", 0),
                _value(event, "item"),
                final=False,
            )
            return

        if event_type == "response.output_item.done":
            self._feed_item(
                _value(event, "output_index", 0),
                _value(event, "item"),
                final=True,
            )
            return

        if event_type == "response.function_call_arguments.delta":
            self._saw_tool_call = True
            call = self._tool_call(_value(event, "output_index", 0))
            delta = _value(event, "delta", "")
            if isinstance(delta, str):
                call.arguments += delta
                call.saw_argument_delta = True
            return

        if event_type == "response.function_call_arguments.done":
            self._saw_tool_call = True
            call = self._tool_call(_value(event, "output_index", 0))
            arguments = _value(event, "arguments")
            name = _value(event, "name")
            if isinstance(arguments, str) and arguments:
                call.arguments = arguments
            if isinstance(name, str) and name:
                call.name = name
            return

        if event_type == "response.completed":
            self._feed_completed(_value(event, "response"))
            return

        if event_type == "error":
            message = _value(event, "message", "OpenAI Responses stream failed")
            raise RuntimeError(str(message))

        if event_type == "response.failed":
            response = _value(event, "response")
            error = _value(response, "error")
            message = _value(error, "message", "OpenAI Responses request failed")
            raise RuntimeError(str(message))

    def finalize(self) -> ModelResponse:
        tool_calls: list[ToolCall] = []
        for index, call in sorted(self._tools.items()):
            if not call.name:
                continue
            tool_calls.append(
                ToolCall(
                    id=call.call_id or f"call_{index}",
                    name=call.name,
                    arguments=json.loads(call.arguments or "{}"),
                )
            )
        return ModelResponse(content="".join(self._content), tool_calls=tool_calls)

    def _feed_item(self, index: int, item: Any, *, final: bool) -> None:
        if _value(item, "type") != "function_call":
            return
        self._saw_tool_call = True
        call = self._tool_call(index)
        call_id = _value(item, "call_id") or _value(item, "id")
        name = _value(item, "name")
        arguments = _value(item, "arguments")
        if isinstance(call_id, str) and call_id:
            call.call_id = call_id
        if isinstance(name, str) and name:
            call.name = name
        if isinstance(arguments, str) and arguments:
            if final or not call.saw_argument_delta:
                call.arguments = arguments

    def _feed_completed(self, response: Any) -> None:
        output = _value(response, "output", ())
        if not isinstance(output, (list, tuple)):
            return
        for index, item in enumerate(output):
            if _value(item, "type") == "message" and not self._content:
                content = _value(item, "content", ())
                if not isinstance(content, (list, tuple)):
                    continue
                for part in content:
                    if _value(part, "type") != "output_text":
                        continue
                    text = _value(part, "text", "")
                    if isinstance(text, str) and text:
                        self._content.append(text)
                        if self._on_token is not None and not self._saw_tool_call:
                            self._on_token(text)
            else:
                self._feed_item(index, item, final=True)

    def _tool_call(self, index: Any) -> _ResponsesToolCall:
        key = index if isinstance(index, int) else 0
        return self._tools.setdefault(key, _ResponsesToolCall())


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
