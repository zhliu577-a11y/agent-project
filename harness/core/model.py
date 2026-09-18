# core/model.py - model adapter, routing, and capability contracts
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from core.types import Message, ModelResponse


@dataclass(frozen=True)
class ModelMetadata:
    """Provider capabilities consumed by model routers."""

    roles: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    context_window: int | None = None
    supports_tools: bool = True
    supports_streaming: bool = True
    cost_tier: str = "unknown"
    latency_tier: str = "unknown"

    def supports(self, capability: str) -> bool:
        if capability == "tools":
            return self.supports_tools
        if capability == "streaming":
            return self.supports_streaming
        return capability in self.capabilities


@dataclass(frozen=True)
class ModelProviderInfo:
    """Read-only provider metadata exposed to a router."""

    name: str
    metadata: ModelMetadata = field(default_factory=ModelMetadata)


@dataclass(frozen=True)
class ModelRouteRequest:
    """One model-selection request created by the gateway."""

    role: str | None
    requested_model: str | None
    default_model: str
    fallback: tuple[str, ...]
    routes: Mapping[str, tuple[str, ...]]
    messages: tuple[Message, ...]
    tool_schemas: tuple[dict[str, object], ...]
    providers: tuple[ModelProviderInfo, ...]


@dataclass(frozen=True)
class ModelRouteDecision:
    """Ordered provider candidates; the gateway tries them left to right."""

    candidates: tuple[str, ...]
    reason: str = ""


class ModelRouter(ABC):
    """Trusted model-router contract used before each model call."""

    @abstractmethod
    def route(self, request: ModelRouteRequest) -> ModelRouteDecision:
        """Return ordered provider names for this request."""
        ...


class ModelAdapter(ABC):
    """所有模型后端（OpenAI、DeepSeek、本地 vLLM 等）都要实现此接口。"""

    @abstractmethod
    async def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
        on_token: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        """把消息历史和工具清单发给模型，返回模型的回复。

        on_token：可选回调；模型以流式返回纯文本时，每个增量都会调用它。
        模型决定调用工具时不会触发（工具调用增量静默收集）。
        """
        ...
