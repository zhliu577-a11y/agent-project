"""Built-in event-transport.v1 implementation for immediate delivery."""

from core.events import Event, EventDispatcher, EventTransport


class InlineTransport(EventTransport):
    """Deliver each event in the caller's task without queueing."""

    def __init__(self) -> None:
        self._dispatch: EventDispatcher | None = None
        self._closed = False

    def bind(self, dispatch: EventDispatcher) -> None:
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        if self._dispatch is not None and self._dispatch is not dispatch:
            raise RuntimeError("transport is already bound to an event gateway")
        self._dispatch = dispatch

    async def send(self, event: Event) -> None:
        if self._closed:
            raise RuntimeError("event transport is closed")
        if self._dispatch is None:
            raise RuntimeError("event transport is not bound to an event gateway")
        await self._dispatch(event)

    async def flush(self) -> None:
        return None

    async def stop(self) -> None:
        self._closed = True


def create_transport(plugin_dir, context=None):
    return InlineTransport()
