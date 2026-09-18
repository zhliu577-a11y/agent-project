# core/hooks.py - typed in-process hook gateway
import asyncio
import fnmatch
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from core.events import Event, EventGateway, EventIdentity, EventSubscriber
from core.types import ModelResponse, ToolCall, TurnContext

logger = logging.getLogger(__name__)

HookDecision = Literal["allow", "ask", "deny"]
HookEvent = Literal[
    "user_prompt_submit",
    "turn_start",
    "llm_response",
    "tool_before",
    "tool_after",
    "turn_end",
]
HookFailurePolicy = Literal["allow", "deny"]

HOOK_EVENTS: tuple[HookEvent, ...] = (
    "user_prompt_submit",
    "turn_start",
    "llm_response",
    "tool_before",
    "tool_after",
    "turn_end",
)

ConfirmFn = Callable[[TurnContext, ToolCall], Awaitable[bool]]

_BRIDGED_EVENTS = ("turn.start", "model.response", "tool.after", "turn.end")


@dataclass(frozen=True)
class PromptRequest:
    """A user prompt presented to the control-plane policy chain."""

    text: str
    session_id: str | None = None


PromptConfirmFn = Callable[[PromptRequest], Awaitable[bool]]


@dataclass(frozen=True)
class HookSpec:
    """Declarative scheduling contract for one internal hook plugin."""

    events: tuple[HookEvent, ...] = HOOK_EVENTS
    matcher: str = "*"
    priority: int = 0
    timeout: float = 5.0
    on_error: HookFailurePolicy | None = None

    def handles(self, event: HookEvent) -> bool:
        return event in self.events

    def matches(self, value: str | None = None) -> bool:
        return value is None or fnmatch.fnmatch(value, self.matcher)

    def failure_decision(self, event: HookEvent) -> HookFailurePolicy:
        if self.on_error is not None:
            return self.on_error
        return "deny" if event in {"tool_before", "user_prompt_submit"} else "allow"


@dataclass(frozen=True)
class HookResult:
    """Optional structured result returned by decision-capable hooks."""

    decision: HookDecision = "allow"
    reason: str = ""


class LifecycleHooks:
    """Base class for internal hook plugins."""

    async def setup(self, context: object) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def user_prompt_submit(
        self,
        request: PromptRequest,
    ) -> HookDecision | HookResult | bool | None:
        return "allow"

    async def turn_start(self, ctx: TurnContext) -> None: ...
    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None: ...

    async def tool_before(
        self,
        ctx: TurnContext,
        tool_call: ToolCall,
    ) -> HookDecision | HookResult | bool | None:
        return "allow"

    async def tool_after(
        self,
        ctx: TurnContext,
        tool_call: ToolCall,
        result: Any,
        ok: bool,
    ) -> None: ...

    async def turn_end(self, ctx: TurnContext) -> None: ...


async def _default_confirm(ctx: TurnContext, tool_call: ToolCall) -> bool:
    """Ask the interactive client for confirmation."""
    logger.info("requesting confirmation for tool call: %s", tool_call.name)
    answer = (
        input(f"[permission] allow {tool_call.name} with arguments {tool_call.arguments}? [y/N]: ")
        .strip()
        .lower()
    )
    return answer in {"y", "yes"}


async def _default_prompt_confirm(request: PromptRequest) -> bool:
    """Ask the interactive client whether one submitted prompt may continue."""
    logger.info("requesting confirmation for user prompt")
    answer = input("[permission] allow this user prompt? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


@dataclass(frozen=True)
class _HookRegistration:
    name: str
    spec: HookSpec
    order: int
    hook: LifecycleHooks


class HookGateway:
    """Ordered, isolated in-process fan-out for hook plugins."""

    def __init__(self) -> None:
        self._hooks: list[_HookRegistration] = []
        self._attached_event_views: set[int] = set()

    def add(
        self,
        hook: LifecycleHooks,
        priority: int = 0,
        *,
        name: str | None = None,
        spec: HookSpec | None = None,
    ) -> None:
        """Register a hook.

        ``priority`` remains for backward compatibility. A supplied ``spec`` is
        the authoritative declarative scheduling contract.
        """
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"priority must be an integer, got: {priority!r}")
        resolved_name = name or type(hook).__name__
        resolved_spec = spec or HookSpec(priority=priority)
        if resolved_spec.priority != priority and spec is None:
            resolved_spec = HookSpec(
                events=resolved_spec.events,
                matcher=resolved_spec.matcher,
                priority=priority,
                timeout=resolved_spec.timeout,
                on_error=resolved_spec.on_error,
            )
        if name is not None and any(item.name == resolved_name for item in self._hooks):
            raise ValueError(f"duplicate hook plugin name: {resolved_name}")
        self._hooks.append(
            _HookRegistration(
                name=resolved_name,
                spec=resolved_spec,
                order=len(self._hooks),
                hook=hook,
            )
        )

    @property
    def count(self) -> int:
        return len(self._hooks)

    def attach(
        self,
        events: EventSubscriber | EventGateway,
        *,
        priority: int = 100,
    ) -> None:
        """Bridge observation events from one gateway or subscriber view once."""
        attach_key = id(events)
        if attach_key in self._attached_event_views:
            return
        subscriber = (
            events
            if isinstance(events, EventSubscriber)
            else events.subscriber(EventIdentity.host("hook-gateway"))
        )
        for name in _BRIDGED_EVENTS:
            subscriber.subscribe(name, self._on_observation_event, priority=priority)
        self._attached_event_views.add(attach_key)
        logger.info(
            "HookGateway attached as %s (%d events)",
            subscriber.identity.subject,
            len(_BRIDGED_EVENTS),
        )

    async def _on_observation_event(self, event: Event) -> None:
        payload = event.payload
        if event.name == "turn.start":
            await self.turn_start(payload["ctx"])
        elif event.name == "model.response":
            await self.llm_response(payload["ctx"], payload["resp"])
        elif event.name == "tool.after":
            await self.tool_after(
                payload["ctx"],
                payload["tool_call"],
                payload["result"],
                payload["ok"],
            )
        elif event.name == "turn.end":
            await self.turn_end(payload["ctx"])

    def _ordered(self, event: HookEvent, subject: str | None = None) -> list[_HookRegistration]:
        return sorted(
            (
                registration
                for registration in self._hooks
                if registration.spec.handles(event) and registration.spec.matches(subject)
            ),
            key=lambda item: (item.spec.priority, item.order),
        )

    async def _invoke(self, registration: _HookRegistration, event: HookEvent, awaitable):
        try:
            return await asyncio.wait_for(awaitable, timeout=registration.spec.timeout)
        except TimeoutError:
            logger.error(
                "hook %s timed out after %.3fs during %s",
                registration.name,
                registration.spec.timeout,
                event,
            )
            raise

    async def _fold_decision(
        self,
        event: HookEvent,
        subject: str | None,
        invoke: Callable[[_HookRegistration], Awaitable[object]],
    ) -> HookDecision:
        decision: HookDecision = "allow"
        for registration in self._ordered(event, subject):
            try:
                value = await self._invoke(registration, event, invoke(registration))
                vote = _coerce_decision(value, registration.name)
            except Exception as exc:
                failure = registration.spec.failure_decision(event)
                logger.exception(
                    "%s hook failed: %s: %s (policy=%s)",
                    event,
                    registration.name,
                    exc,
                    failure,
                )
                vote = failure

            if vote == "deny":
                decision = "deny"
            elif decision == "allow" and vote == "ask":
                decision = "ask"
        return decision

    async def user_prompt_submit(
        self,
        request: PromptRequest,
        confirm: PromptConfirmFn | None = None,
    ) -> bool:
        """Run the control-plane policy chain before entering the agent loop."""
        decision = await self._fold_decision(
            "user_prompt_submit",
            request.session_id,
            lambda registration: registration.hook.user_prompt_submit(request),
        )
        if decision == "deny":
            logger.warning("user prompt denied by hook gateway")
            return False
        if decision == "ask":
            asker = confirm if confirm is not None else _default_prompt_confirm
            try:
                return await asker(request)
            except Exception as exc:
                logger.exception("user prompt confirmation failed; denying: %s", exc)
                return False
        return True

    async def turn_start(self, ctx: TurnContext) -> None:
        for registration in self._ordered("turn_start"):
            try:
                await self._invoke(registration, "turn_start", registration.hook.turn_start(ctx))
            except Exception as exc:
                logger.exception("turn_start hook failed: %s: %s", registration.name, exc)

    async def llm_response(self, ctx: TurnContext, resp: ModelResponse) -> None:
        for registration in self._ordered("llm_response"):
            try:
                await self._invoke(
                    registration,
                    "llm_response",
                    registration.hook.llm_response(ctx, resp),
                )
            except Exception as exc:
                logger.exception("llm_response hook failed: %s: %s", registration.name, exc)

    async def tool_before(
        self,
        ctx: TurnContext,
        tool_call: ToolCall,
        confirm: ConfirmFn | None = None,
    ) -> bool:
        """Collect decisions using deny > ask > allow, then ask at most once."""
        subject = getattr(tool_call, "name", None)
        decision = await self._fold_decision(
            "tool_before",
            subject,
            lambda registration: registration.hook.tool_before(ctx, tool_call),
        )

        if decision == "deny":
            logger.warning("tool call denied by hook gateway: %s", tool_call.name)
            return False
        if decision == "ask":
            asker = confirm if confirm is not None else _default_confirm
            try:
                return await asker(ctx, tool_call)
            except Exception as exc:
                logger.exception("ask confirmation failed; denying: %s", exc)
                return False
        return True

    async def tool_after(
        self,
        ctx: TurnContext,
        tool_call: ToolCall,
        result: Any,
        ok: bool,
    ) -> None:
        subject = getattr(tool_call, "name", None)
        for registration in self._ordered("tool_after", subject):
            try:
                await self._invoke(
                    registration,
                    "tool_after",
                    registration.hook.tool_after(ctx, tool_call, result, ok),
                )
            except Exception as exc:
                logger.exception("tool_after hook failed: %s: %s", registration.name, exc)

    async def turn_end(self, ctx: TurnContext) -> None:
        for registration in self._ordered("turn_end"):
            try:
                await self._invoke(registration, "turn_end", registration.hook.turn_end(ctx))
            except Exception as exc:
                logger.exception("turn_end hook failed: %s: %s", registration.name, exc)


def _coerce_decision(value: Any, name: str) -> HookDecision:
    if isinstance(value, HookResult):
        value = value.decision
    if value is True:
        return "allow"
    if value is False:
        return "deny"
    if value is None:
        return "allow"
    if value not in ("allow", "ask", "deny"):
        logger.warning("hook %s returned invalid decision %r; denying", name, value)
        return "deny"
    return value
