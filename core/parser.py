# core/parser.py —— Response Parser 抽象：把厂商流式分片翻译成 ModelResponse
#
# 定位：内核提供的“协议解析能力”（与 errors/types 同级），不是插件 kind。
# - ResponseParser：插件只写传输，解析委托给本层；
# - ToolCallAccumulator：按 index 合并分片到达的 id/name/arguments；
# - OpenAICompatibleParser：OpenAI 兼容 chat.completions 流的通用实现，
#   deepseek / openai / vLLM 等插件可直接复用；协议不同则另写一个 parser。
import json
from abc import ABC, abstractmethod
from collections.abc import Callable
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
