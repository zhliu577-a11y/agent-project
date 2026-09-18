# tests/test_model_gateway.py - lazy model providers, routing, fallback, lifecycle
from pathlib import Path

import pytest

from core.errors import ModelError
from core.model import ModelAdapter, ModelMetadata, ModelRouteDecision, ModelRouter
from core.retry import RetryDecision, RetryExecutor, RetryRequest
from core.types import Message, ModelResponse
from gateways.model_gateway import ModelGateway
from plugins.context import PluginContext
from plugins.loader import ModelPlugin, PluginManifest

pytestmark = pytest.mark.asyncio


def _manifest(name: str, *, metadata: ModelMetadata | None = None) -> PluginManifest:
    return PluginManifest(
        name=name,
        type="model",
        version="1.0.0",
        description="",
        enabled=True,
        directory=Path("."),
        entry={"module": "model.py", "factory": "create_model"},
        model=metadata or ModelMetadata(),
    )


class FakeAdapter(ModelAdapter):
    def __init__(
        self,
        name: str,
        trace: list[str],
        *,
        response: str | None = None,
        error: Exception | None = None,
        emit_before_error: bool = False,
    ) -> None:
        self.name = name
        self.trace = trace
        self.response = response or name
        self.error = error
        self.emit_before_error = emit_before_error

    async def setup(self, context) -> None:
        self.trace.append(f"setup:{self.name}")

    async def start(self) -> None:
        self.trace.append(f"start:{self.name}")

    async def stop(self) -> None:
        self.trace.append(f"stop:{self.name}")

    async def complete(self, messages, tool_schemas, on_token=None):
        self.trace.append(f"complete:{self.name}")
        if self.error is not None:
            if self.emit_before_error and on_token is not None:
                on_token("partial")
            raise self.error
        if on_token is not None:
            on_token(self.response)
        return ModelResponse(content=self.response, tool_calls=[])


def _provider(
    name: str,
    trace: list[str],
    *,
    response: str | None = None,
    error: Exception | None = None,
    emit_before_error: bool = False,
    metadata: ModelMetadata | None = None,
) -> ModelPlugin:
    return ModelPlugin(
        manifest=_manifest(name, metadata=metadata),
        factory=lambda _directory: FakeAdapter(
            name,
            trace,
            response=response,
            error=error,
            emit_before_error=emit_before_error,
        ),
        context=PluginContext.create(
            name=name,
            kind="model",
            directory=Path("."),
        ),
    )


async def test_gateway_prewarms_and_unloads_a_provider() -> None:
    trace: list[str] = []
    gateway = ModelGateway([_provider("a", trace)], default_model="a")

    assert gateway.status() == {"a": "idle"}
    await gateway.prewarm("a")
    assert gateway.status() == {"a": "active"}
    assert trace == ["setup:a", "start:a"]

    await gateway.unload("a")
    assert gateway.status() == {"a": "idle"}
    assert trace == ["setup:a", "start:a", "stop:a"]


async def test_gateway_falls_back_before_streaming_starts() -> None:
    trace: list[str] = []
    gateway = ModelGateway(
        [
            _provider("bad", trace, error=RuntimeError("boom")),
            _provider("good", trace, response="fallback"),
        ],
        default_model="bad",
        fallback=("good",),
    )

    response = await gateway.complete([], [])

    assert response.content == "fallback"
    assert trace == [
        "setup:bad",
        "start:bad",
        "complete:bad",
        "setup:good",
        "start:good",
        "complete:good",
    ]


async def test_gateway_does_not_fallback_after_streaming_starts() -> None:
    trace: list[str] = []
    gateway = ModelGateway(
        [
            _provider(
                "bad",
                trace,
                error=RuntimeError("stream broke"),
                emit_before_error=True,
            ),
            _provider("good", trace, response="should not run"),
        ],
        default_model="bad",
        fallback=("good",),
    )

    with pytest.raises(ModelError, match="after streaming started"):
        await gateway.complete([], [], on_token=lambda _text: None)

    assert "complete:good" not in trace


async def test_gateway_routes_by_agent_role() -> None:
    trace: list[str] = []
    gateway = ModelGateway(
        [_provider("default", trace), _provider("coding", trace)],
        default_model="default",
        routes={"coding": ("coding",)},
    )

    with gateway.role("coding"):
        response = await gateway.complete([Message(role="user", content="hi")], [])

    assert response.content == "coding"
    assert gateway.active_model == "default"


async def test_explicit_use_overrides_role() -> None:
    trace: list[str] = []
    gateway = ModelGateway(
        [_provider("default", trace), _provider("coding", trace)],
        default_model="default",
        routes={"coding": ("coding",)},
    )

    await gateway.use("default")
    with gateway.role("coding"):
        response = await gateway.complete([], [])

    assert response.content == "default"
    assert gateway.active_model == "default"


async def test_failed_explicit_switch_keeps_previous_model() -> None:
    trace: list[str] = []

    class FailingSetupAdapter(FakeAdapter):
        async def setup(self, context) -> None:
            self.trace.append(f"setup:{self.name}")
            raise RuntimeError("setup failed")

    failing = ModelPlugin(
        manifest=_manifest("failing"),
        factory=lambda _directory: FailingSetupAdapter("failing", trace),
        context=PluginContext.create(
            name="failing",
            kind="model",
            directory=Path("."),
        ),
    )
    gateway = ModelGateway(
        [_provider("working", trace), failing],
        default_model="working",
    )

    await gateway.use("working")
    with pytest.raises(ModelError, match="initialization failed"):
        await gateway.use("failing")

    assert gateway.active_model == "working"
    assert gateway.status()["working"] == "active"
    assert gateway.status()["failing"] == "error"


async def test_gateway_rejects_router_result_with_unknown_provider() -> None:
    class BrokenRouter(ModelRouter):
        def route(self, request):
            return ModelRouteDecision(candidates=("missing",))

    trace: list[str] = []
    gateway = ModelGateway(
        [_provider("a", trace)],
        default_model="a",
        router=BrokenRouter(),
    )

    with pytest.raises(ModelError, match="unknown providers"):
        await gateway.complete([], [])


async def test_gateway_exposes_provider_capabilities() -> None:
    trace: list[str] = []
    metadata = ModelMetadata(
        roles=("coding",),
        capabilities=("text", "vision"),
        context_window=128000,
        cost_tier="low",
    )
    gateway = ModelGateway(
        [_provider("a", trace, metadata=metadata)],
        default_model="a",
    )

    assert gateway.provider_info()[0].metadata.supports("vision")
    assert gateway.provider_info()[0].metadata.context_window == 128000


async def test_gateway_stops_partially_initialized_provider() -> None:
    trace: list[str] = []

    class FailingSetupAdapter(FakeAdapter):
        async def setup(self, context) -> None:
            self.trace.append(f"setup:{self.name}")
            raise RuntimeError("setup failed")

    plugin = ModelPlugin(
        manifest=_manifest("broken"),
        factory=lambda _directory: FailingSetupAdapter("broken", trace),
        context=PluginContext.create(
            name="broken",
            kind="model",
            directory=Path("."),
        ),
    )
    gateway = ModelGateway([plugin], default_model="broken")

    with pytest.raises(ModelError, match="initialization failed"):
        await gateway.prewarm("broken")

    assert trace == ["setup:broken", "stop:broken"]
    assert gateway.status() == {"broken": "error"}


async def test_gateway_uses_retry_policy_before_fallback() -> None:
    trace: list[str] = []

    class FlakyAdapter(FakeAdapter):
        def __init__(self) -> None:
            super().__init__("flaky", trace)
            self.calls = 0

        async def complete(self, messages, tool_schemas, on_token=None):
            self.calls += 1
            self.trace.append(f"complete:{self.name}")
            if self.calls < 2:
                raise TimeoutError("transient")
            return ModelResponse(content="retried", tool_calls=[])

    class AllowRetry:
        async def decide(self, request: RetryRequest) -> RetryDecision:
            return RetryDecision(retry=True, delay=0, reason="test")

    plugin = ModelPlugin(
        manifest=_manifest("flaky"),
        factory=lambda _directory: FlakyAdapter(),
        context=PluginContext.create(
            name="flaky",
            kind="model",
            directory=Path("."),
        ),
    )
    retry = RetryExecutor(
        {"allow": AllowRetry()},
        default_policy="allow",
        max_attempts=2,
        max_delay=0,
        total_timeout=1,
    )
    gateway = ModelGateway([plugin], default_model="flaky", retry=retry)

    response = await gateway.complete([], [])

    assert response.content == "retried"
    assert trace == [
        "setup:flaky",
        "start:flaky",
        "complete:flaky",
        "complete:flaky",
    ]
