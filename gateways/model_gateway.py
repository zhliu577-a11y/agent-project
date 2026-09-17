# gateways/model_gateway.py - lazy model providers, routing, fallback, lifecycle
import asyncio
import contextvars
import inspect
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from core.errors import ModelError, translate_error
from core.model import (
    ModelAdapter,
    ModelProviderInfo,
    ModelRouteDecision,
    ModelRouter,
    ModelRouteRequest,
)
from core.retry import RetryExecutor
from core.types import Message, ModelResponse
from plugins.loader import ModelPlugin

logger = logging.getLogger(__name__)


class _DefaultModelRouter(ModelRouter):
    """Config-driven default: explicit override, role route, then fallback."""

    def route(self, request: ModelRouteRequest) -> ModelRouteDecision:
        if request.requested_model is not None:
            return ModelRouteDecision(
                candidates=_dedupe((request.requested_model, *request.fallback)),
                reason="requested model override",
            )

        role_route = request.routes.get(request.role) if request.role is not None else None
        base = role_route or (request.default_model,)
        return ModelRouteDecision(
            candidates=_dedupe((*base, *request.fallback)),
            reason=f"role route: {request.role}" if role_route else "default model",
        )


@dataclass
class _ProviderSlot:
    plugin: ModelPlugin
    instance: ModelAdapter | None = None
    state: str = "idle"
    refs: int = 0
    error: str | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)


class ModelGateway(ModelAdapter):
    """The single model dependency consumed by the unchanged agent loop.

    Providers are created lazily, kept warm per configuration, and stopped
    through their optional lifecycle methods. Routing and fallback happen
    inside ``complete`` and therefore stay invisible to the loop.
    """

    def __init__(
        self,
        providers: Sequence[ModelPlugin],
        *,
        default_model: str,
        fallback: Sequence[str] = (),
        routes: Mapping[str, Sequence[str]] | None = None,
        router: ModelRouter | None = None,
        retry: RetryExecutor | None = None,
    ) -> None:
        if not providers:
            raise ValueError("ModelGateway requires at least one model provider")
        self._slots = {plugin.manifest.name: _ProviderSlot(plugin) for plugin in providers}
        if len(self._slots) != len(providers):
            raise ValueError("duplicate model provider name")
        if default_model not in self._slots:
            raise ValueError(
                f"unknown default model {default_model!r}; available: {sorted(self._slots)}"
            )

        self._default_model = default_model
        self._fallback = tuple(fallback)
        self._routes = MappingProxyType(
            {name: tuple(candidates) for name, candidates in (routes or {}).items()}
        )
        self._router = router or _DefaultModelRouter()
        self._retry = retry
        self._active_model: str | None = None
        self._role: contextvars.ContextVar[str | None] = contextvars.ContextVar(
            f"model_role_{id(self)}",
            default=None,
        )
        self._closed = False
        self._last_errors: dict[str, str] = {}

        unknown = {
            name
            for candidates in (self._fallback, *self._routes.values())
            for name in candidates
            if name not in self._slots
        }
        if unknown:
            raise ValueError(
                f"model routing references unknown providers: {sorted(unknown)}; "
                f"available: {sorted(self._slots)}"
            )

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def active_model(self) -> str:
        return self._active_model or self._default_model

    def available(self) -> tuple[str, ...]:
        return tuple(sorted(self._slots))

    def status(self) -> dict[str, str]:
        return {
            name: "error" if slot.error else slot.state
            for name, slot in sorted(self._slots.items())
        }

    def provider_info(self) -> tuple[ModelProviderInfo, ...]:
        return tuple(
            ModelProviderInfo(
                name=name,
                metadata=slot.plugin.manifest.model,
            )
            for name, slot in sorted(self._slots.items())
            if slot.plugin.manifest.model is not None
        )

    @contextmanager
    def role(self, name: str | None) -> Iterator[None]:
        """Bind an agent role for model calls made in this async context."""
        token = self._role.set(name)
        try:
            yield
        finally:
            self._role.reset(token)

    async def use(self, name: str, *, prewarm: bool = True) -> None:
        """Switch the explicit active provider, optionally initializing it now."""
        self._require_provider(name)
        if prewarm:
            await self.prewarm(name)
        self._active_model = name

    async def prewarm(self, name: str) -> None:
        """Create, setup, and start one provider without issuing a request."""
        try:
            await self._acquire(name)
        finally:
            await self._release(name)

    async def unload(self, name: str) -> None:
        """Stop one provider after all in-flight requests have released it."""
        slot = self._require_provider(name)
        async with slot.condition:
            while slot.refs:
                await slot.condition.wait()
            if slot.instance is None:
                slot.state = "idle"
                slot.error = None
                return
            slot.state = "stopping"
            instance = slot.instance
            slot.instance = None
            try:
                await _stop_instance(instance)
            except Exception as exc:
                logger.exception("model provider stop failed: %s", name)
                slot.error = str(exc)
                slot.state = "stopped"
            else:
                slot.error = None
                slot.state = "idle"
            slot.condition.notify_all()
        logger.info("model provider unloaded: %s", name)

    async def complete(
        self,
        messages: list[Message],
        tool_schemas: list[dict[str, object]],
        on_token: Callable[[str], None] | None = None,
    ) -> ModelResponse:
        if self._closed:
            raise ModelError("model gateway is closed")

        request = ModelRouteRequest(
            role=self._role.get(),
            requested_model=self._active_model,
            default_model=self._default_model,
            fallback=self._fallback,
            routes=self._routes,
            messages=tuple(messages),
            tool_schemas=tuple(tool_schemas),
            providers=self.provider_info(),
        )
        try:
            decision = self._router.route(request)
        except Exception as exc:
            raise translate_error(
                exc,
                context="model router failed",
                fallback=ModelError,
            ) from exc
        if not isinstance(decision, ModelRouteDecision):
            raise ModelError(
                f"model router must return ModelRouteDecision, got {type(decision).__name__}"
            )
        candidates = _dedupe(decision.candidates)
        if not candidates:
            raise ModelError("model router returned no candidates")
        unknown = [name for name in candidates if name not in self._slots]
        if unknown:
            raise ModelError(f"model router selected unknown providers: {unknown}")

        failures: list[str] = []
        for name in candidates:
            stream_state = {"started": False}

            def _on_token(text: str, state=stream_state) -> None:
                state["started"] = True
                if on_token is not None:
                    on_token(text)

            async def _invoke(provider_name=name):
                model = await self._acquire(provider_name)
                try:
                    return await model.complete(
                        messages,
                        tool_schemas,
                        on_token=_on_token if on_token is not None else None,
                    )
                finally:
                    await self._release(provider_name)

            try:
                if self._retry is None:
                    response = await _invoke()
                else:
                    response = await self._retry.execute(
                        _invoke,
                        operation="model.complete",
                        component=name,
                        retry_safe=True,
                        stream_started=lambda state=stream_state: bool(state["started"]),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_errors[name] = str(exc)
                failures.append(f"{name}: {exc}")
                logger.warning(
                    "model provider %s failed%s: %s",
                    name,
                    " after streaming output" if stream_state["started"] else "",
                    exc,
                )
                if stream_state["started"]:
                    raise translate_error(
                        exc,
                        context=f"model provider {name} failed after streaming started",
                        fallback=ModelError,
                    ) from exc
            else:
                self._last_errors.pop(name, None)
                return response

        raise ModelError(f"all model candidates failed [{decision.reason}]: " + "; ".join(failures))

    async def close(self) -> None:
        """Stop every provider and mark the gateway unusable."""
        if self._closed:
            return
        self._closed = True
        for name in list(self._slots):
            try:
                await self.unload(name)
            except Exception:
                logger.exception("model provider cleanup failed: %s", name)

    def _require_provider(self, name: str) -> _ProviderSlot:
        slot = self._slots.get(name)
        if slot is None:
            raise ModelError(f"unknown model provider {name!r}; available: {sorted(self._slots)}")
        return slot

    async def _acquire(self, name: str) -> ModelAdapter:
        if self._closed:
            raise ModelError("model gateway is closed")
        slot = self._require_provider(name)
        async with slot.condition:
            if slot.instance is None:
                slot.state = "loading"
                slot.condition.notify_all()
                instance: ModelAdapter | None = None
                try:
                    instance = slot.plugin.create()
                    await _setup_instance(instance, slot.plugin.context)
                    await _start_instance(instance)
                except asyncio.CancelledError:
                    if instance is not None:
                        await _stop_quietly(instance)
                    slot.state = "error"
                    slot.error = "initialization cancelled"
                    slot.condition.notify_all()
                    raise
                except Exception as exc:
                    if instance is not None:
                        await _stop_quietly(instance)
                    slot.state = "error"
                    slot.error = str(exc)
                    slot.condition.notify_all()
                    raise translate_error(
                        exc,
                        context=f"model provider {name} initialization failed",
                        fallback=ModelError,
                    ) from exc
                slot.instance = instance
                slot.state = "active"
                slot.error = None
                slot.condition.notify_all()
            slot.refs += 1
            return slot.instance

    async def _release(self, name: str) -> None:
        slot = self._slots.get(name)
        if slot is None:
            return
        async with slot.condition:
            if slot.refs > 0:
                slot.refs -= 1
            slot.condition.notify_all()


def _dedupe(names: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for name in names:
        if name not in result:
            result.append(name)
    return tuple(result)


async def _call(callback: Callable[..., Any], *args: object) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


async def _setup_instance(instance: object, context: object | None) -> None:
    setup = getattr(instance, "setup", None)
    if callable(setup) and context is not None:
        await _call(setup, context)


async def _start_instance(instance: object) -> None:
    start = getattr(instance, "start", None)
    if callable(start):
        await _call(start)


async def _stop_instance(instance: object) -> None:
    stop = getattr(instance, "stop", None)
    if callable(stop):
        await _call(stop)


async def _stop_quietly(instance: object) -> None:
    try:
        await _stop_instance(instance)
    except Exception:
        logger.exception("model provider rollback failed")
