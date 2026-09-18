# core/events.py - host-owned event routing and transport contracts.
#
# EventGateway owns subscriptions, scope filtering, ordering, identity binding,
# and handler isolation. EventTransport plugins only move already-built
# observation events and call back into the gateway for local delivery.
import asyncio
import inspect
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from core.tracing import current_trace_id

logger = logging.getLogger(__name__)

Handler = Callable[["Event"], Any]
EventDispatcher = Callable[["Event"], Awaitable[None]]
OverflowPolicy = Literal["block", "drop_newest", "drop_oldest"]
IdentityKind = Literal["host", "plugin", "agent"]
WILDCARD = "*"


@dataclass(frozen=True)
class EventIdentity:
    """Trusted publisher/subscriber identity supplied by the host."""

    subject: str
    kind: IdentityKind
    name: str
    version: str | None = None
    package_name: str | None = None
    contribution_id: str | None = None

    @classmethod
    def host(cls, name: str) -> "EventIdentity":
        if not name.strip():
            raise ValueError("host identity name must not be empty")
        return cls(subject=f"host:{name}", kind="host", name=name)

    @classmethod
    def plugin(cls, manifest: object) -> "EventIdentity":
        kind = getattr(manifest, "type", None)
        name = getattr(manifest, "name", None)
        if not isinstance(kind, str) or not kind:
            raise ValueError("plugin manifest is missing a valid type")
        if not isinstance(name, str) or not name:
            raise ValueError("plugin manifest is missing a valid name")
        return cls(
            subject=f"plugin:{kind}:{name}",
            kind="plugin",
            name=name,
            version=getattr(manifest, "version", None),
            package_name=getattr(manifest, "package_name", None),
            contribution_id=getattr(manifest, "contribution_id", None),
        )

    @classmethod
    def agent(cls, agent_id: str) -> "EventIdentity":
        if not agent_id.strip():
            raise ValueError("agent identity id must not be empty")
        return cls(subject=f"agent:{agent_id}", kind="agent", name=agent_id)


@dataclass(frozen=True)
class EventScope:
    """Optional run/session/agent filter attached to a subscription."""

    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    trace_id: str | None = None

    def matches(self, event: "Event") -> bool:
        for name in (
            "session_id",
            "run_id",
            "turn_id",
            "agent_id",
            "trace_id",
        ):
            expected = getattr(self, name)
            if expected is not None and getattr(event, name) != expected:
                return False
        return True


@dataclass(frozen=True)
class Event:
    """A structured observation event."""

    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    event_id: str = field(default_factory=lambda: uuid4().hex)
    session_id: str | None = None
    run_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    publisher: EventIdentity | None = None


@dataclass(frozen=True)
class _Subscriber:
    priority: int
    order: int
    handler: Handler
    owner: EventIdentity
    scope: EventScope | None = None


@dataclass(frozen=True)
class Subscription:
    """A listener plugin's declared event subscription."""

    event: str
    handler: Handler
    priority: int = 0
    scope: EventScope | None = None


@dataclass(frozen=True)
class SubscriptionInfo:
    """JSON-safe management snapshot of one active subscription."""

    event: str
    priority: int
    order: int
    owner: EventIdentity
    scope: EventScope | None = None


def coerce_subscriptions(value: Any, where: str) -> tuple[Subscription, ...]:
    """Normalize a listener factory result into Subscription objects."""
    if isinstance(value, Subscription):
        items: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError(
            f"{where}: factory must return Subscription or a list of Subscriptions; "
            f"got {type(value).__name__}"
        )
    subscriptions = tuple(items)
    for item in subscriptions:
        if not isinstance(item, Subscription):
            raise ValueError(
                f"{where}: list contains a non-Subscription value: {type(item).__name__}"
            )
        if item.scope is not None and not isinstance(item.scope, EventScope):
            raise ValueError(f"{where}: subscription scope must be EventScope")
    if not subscriptions:
        raise ValueError(f"{where}: factory returned no subscriptions")
    return subscriptions


async def _invoke(handler: Handler, event: Event) -> Any:
    """Invoke sync or async handlers without changing sync return values."""
    result = handler(event)
    if inspect.isawaitable(result):
        return await result
    return result


class EventTransport(ABC):
    """Move events without owning business subscriptions or routing."""

    @abstractmethod
    def bind(self, dispatch: EventDispatcher) -> None:
        """Bind the local gateway callback used for incoming events."""

    @abstractmethod
    async def send(self, event: Event) -> None:
        """Submit one event to the physical transport."""

    @abstractmethod
    async def flush(self) -> None:
        """Wait until events submitted before this call have been delivered."""

    async def setup(self, context: object) -> None:
        """Lifecycle hook compatible with plugin loading."""
        return None

    async def start(self) -> None:
        """Start transport-owned background resources."""
        return None

    @abstractmethod
    async def stop(self) -> None:
        """Flush and release transport-owned resources."""


class InProcessTransport(EventTransport):
    """Bounded asynchronous in-process transport."""

    def __init__(
        self,
        *,
        queue_size: int = 1024,
        overflow: OverflowPolicy = "block",
    ) -> None:
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size <= 0:
            raise ValueError("queue_size must be a positive integer")
        if overflow not in {"block", "drop_newest", "drop_oldest"}:
            raise ValueError("overflow must be one of: block, drop_newest, drop_oldest")

        self.queue_size = queue_size
        self.overflow = overflow
        self.dropped_events = 0
        self._dispatch: EventDispatcher | None = None
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=queue_size)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    def bind(self, dispatch: EventDispatcher) -> None:
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        if self._dispatch is not None and self._dispatch is not dispatch:
            raise RuntimeError("transport is already bound to an event gateway")
        self._dispatch = dispatch

    async def start(self) -> None:
        self._ensure_worker()

    async def send(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("event transport is closed")
        if self._dispatch is None:
            raise RuntimeError("event transport is not bound to an event gateway")
        self._ensure_worker()
        await self._enqueue(event)

    async def flush(self) -> None:
        if self._worker is None:
            return
        await self._queue.join()

    async def stop(self) -> None:
        if self._closed:
            return
        await self.flush()
        worker = self._worker
        if worker is not None and not worker.done():
            await self._queue.put(None)
            await worker
        self._worker = None
        self._closed = True

    async def _enqueue(self, event: Event) -> None:
        if self.overflow == "block":
            await self._queue.put(event)
            return

        try:
            self._queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass

        if self.overflow == "drop_newest":
            self.dropped_events += 1
            logger.warning("event transport queue full; dropping newest event: %s", event.name)
            return

        while True:
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
                self.dropped_events += 1
            except asyncio.QueueEmpty:
                try:
                    self._queue.put_nowait(event)
                    return
                except asyncio.QueueFull:
                    continue

            dropped_name = getattr(dropped, "name", "<shutdown>")
            logger.warning(
                "event transport queue full; dropped oldest event: %s",
                dropped_name,
            )
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                continue

    def _ensure_worker(self) -> None:
        if self._closed:
            raise RuntimeError("event transport is closed")
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run_worker(),
                name="harness-event-transport",
            )

    async def _run_worker(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event is None:
                    return
                assert self._dispatch is not None
                try:
                    await self._dispatch(event)
                except Exception as exc:
                    logger.exception(
                        "event transport delivery failed: name=%s: %s",
                        event.name,
                        exc,
                    )
            finally:
                self._queue.task_done()


class EventPublisher:
    """Publisher-only capability view bound to a trusted identity."""

    def __init__(self, gateway: "EventGateway", identity: EventIdentity) -> None:
        self._gateway = gateway
        self.identity = identity

    async def publish(self, event: Event) -> None:
        await self._gateway.publish(event, publisher=self.identity)


class EventSubscriber:
    """Subscriber-only capability view bound to a trusted identity."""

    def __init__(self, gateway: "EventGateway", identity: EventIdentity) -> None:
        self._gateway = gateway
        self.identity = identity

    def subscribe(
        self,
        name: str,
        handler: Handler,
        *,
        priority: int = 0,
        scope: EventScope | None = None,
    ) -> Callable[[], None]:
        return self._gateway.subscribe(
            name,
            handler,
            priority=priority,
            scope=scope,
            owner=self.identity,
        )

    def subscriptions(self) -> tuple[SubscriptionInfo, ...]:
        return self._gateway.subscriptions(owner=self.identity)

    def subscriber_count(self, name: str) -> int:
        return self._gateway.subscriber_count(name, owner=self.identity)


class EventGateway:
    """Host-owned observation routing and delivery gateway.

    ``publish`` delegates movement to an EventTransport and callback dispatch
    stays here. This service never returns business results and never carries
    control-plane decisions; those belong to direct calls and Hook/Policy
    contracts.
    """

    def __init__(
        self,
        *,
        transport: EventTransport | None = None,
        queue_size: int = 1024,
        overflow: OverflowPolicy = "block",
        handler_timeout: float | None = None,
    ) -> None:
        if handler_timeout is not None and (
            isinstance(handler_timeout, bool)
            or not isinstance(handler_timeout, (int, float))
            or handler_timeout <= 0
        ):
            raise ValueError("handler_timeout must be a positive number or None")

        self.transport = transport or InProcessTransport(
            queue_size=queue_size,
            overflow=overflow,
        )
        self.handler_timeout = float(handler_timeout) if handler_timeout is not None else None
        self._subs: dict[str, list[_Subscriber]] = {}
        self._counter = 0
        self._closed = False
        self.transport.bind(self._dispatch)

    @property
    def queue_size(self) -> int:
        """Compatibility view for the default in-process transport."""
        return getattr(self.transport, "queue_size", 0)

    @property
    def overflow(self) -> OverflowPolicy:
        """Compatibility view for the default in-process transport."""
        return getattr(self.transport, "overflow", "block")

    @property
    def dropped_events(self) -> int:
        """Compatibility view for transports that expose a drop counter."""
        return int(getattr(self.transport, "dropped_events", 0))

    def publisher(self, identity: EventIdentity | None = None) -> EventPublisher:
        return EventPublisher(self, identity or EventIdentity.host("runtime"))

    def subscriber(self, identity: EventIdentity | None = None) -> EventSubscriber:
        return EventSubscriber(self, identity or EventIdentity.host("runtime"))

    async def setup(self, context: object) -> None:
        """Lifecycle hook compatible with plugin loading."""

    async def start(self) -> None:
        await self.transport.start()

    async def stop(self) -> None:
        if self._closed:
            return
        await self.flush()
        await self.transport.stop()
        self._closed = True

    async def close(self) -> None:
        await self.stop()

    async def flush(self) -> None:
        await self.transport.flush()

    def subscribe(
        self,
        name: str,
        handler: Handler,
        *,
        priority: int = 0,
        scope: EventScope | None = None,
        owner: EventIdentity | None = None,
    ) -> Callable[[], None]:
        """Subscribe to one event name, or ``"*"`` for observation events."""
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"priority must be an integer, got: {priority!r}")
        if scope is not None and not isinstance(scope, EventScope):
            raise TypeError("scope must be an EventScope or None")
        self._counter += 1
        resolved_owner = owner or EventIdentity.host("runtime")
        subscriber = _Subscriber(priority, self._counter, handler, resolved_owner, scope)
        self._subs.setdefault(name, []).append(subscriber)

        def unsubscribe() -> None:
            entries = self._subs.get(name)
            if not entries:
                return
            self._subs[name] = [item for item in entries if item is not subscriber]

        return unsubscribe

    def subscriber_count(self, name: str, *, owner: EventIdentity | None = None) -> int:
        entries = self._subs.get(name, [])
        if owner is None:
            return len(entries)
        return sum(1 for subscriber in entries if subscriber.owner == owner)

    def subscriptions(
        self,
        *,
        owner: EventIdentity | None = None,
    ) -> tuple[SubscriptionInfo, ...]:
        """Return a management snapshot; plugins should query only their view."""
        result: list[SubscriptionInfo] = []
        for name, entries in self._subs.items():
            for subscriber in entries:
                if owner is not None and subscriber.owner != owner:
                    continue
                result.append(
                    SubscriptionInfo(
                        event=name,
                        priority=subscriber.priority,
                        order=subscriber.order,
                        owner=subscriber.owner,
                        scope=subscriber.scope,
                    )
                )
        return tuple(sorted(result, key=lambda item: item.order))

    def _ordered(
        self,
        name: str,
        event: Event,
        *,
        include_wildcard: bool = True,
    ) -> list[_Subscriber]:
        merged = list(self._subs.get(name, []))
        if include_wildcard:
            merged.extend(self._subs.get(WILDCARD, []))
        return sorted(
            (
                subscriber
                for subscriber in merged
                if subscriber.scope is None or subscriber.scope.matches(event)
            ),
            key=lambda sub: (sub.priority, sub.order),
        )

    async def publish(
        self,
        event: Event,
        *,
        publisher: EventIdentity | None = None,
    ) -> None:
        """Submit an observation event through the selected transport."""
        if self._closed:
            raise RuntimeError("event gateway is closed")
        event = self._prepare_observation(
            event,
            publisher or EventIdentity.host("runtime"),
        )
        await self.transport.send(event)

    async def _dispatch(self, event: Event) -> None:
        for subscriber in self._ordered(event.name, event):
            try:
                if self.handler_timeout is None:
                    await _invoke(subscriber.handler, event)
                else:
                    await asyncio.wait_for(
                        _invoke(subscriber.handler, event),
                        timeout=self.handler_timeout,
                    )
            except TimeoutError:
                logger.error(
                    "event subscriber timed out after %.3fs: name=%s",
                    self.handler_timeout,
                    event.name,
                )
            except Exception as exc:
                logger.exception(
                    "event subscriber failed: name=%s: %s",
                    event.name,
                    exc,
                )

    @staticmethod
    def _with_trace(event: Event) -> Event:
        if event.trace_id is not None:
            return event
        return replace(event, trace_id=current_trace_id())

    @classmethod
    def _prepare_observation(
        cls,
        event: Event,
        publisher: EventIdentity,
    ) -> Event:
        event = cls._with_trace(event)
        if event.publisher == publisher:
            return event
        return replace(event, publisher=publisher)


# Backward-compatible name for existing internal imports. New code should use
# EventGateway; EventBus is no longer the pluggable contract.
EventBus = EventGateway


def _safe_value(value: Any, limit: int = 200) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_value(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, limit) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:limit] if isinstance(value, str) else value
    text = repr(value)
    return text[:limit]


def jsonl_sink(path: str | Path) -> Handler:
    """Build an observation subscriber that appends events to JSONL."""
    sink_path = Path(path)

    def handler(event: Event) -> None:
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": event.ts,
                "event_id": event.event_id,
                "trace_id": event.trace_id,
                "session_id": event.session_id,
                "run_id": event.run_id,
                "turn_id": event.turn_id,
                "agent_id": event.agent_id,
                "causation_id": event.causation_id,
                "correlation_id": event.correlation_id,
                "publisher": (event.publisher.subject if event.publisher is not None else None),
                "name": event.name,
                "payload": _safe_value(event.payload),
            },
            ensure_ascii=False,
        )
        with sink_path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    return handler
