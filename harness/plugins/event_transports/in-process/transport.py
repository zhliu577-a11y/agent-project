"""Built-in event-transport.v1 implementation."""

from core.events import InProcessTransport


def create_transport(plugin_dir, context=None):
    config = context.config if context is not None else {}
    return InProcessTransport(
        queue_size=config.get("queueSize", 1024),
        overflow=config.get("overflow", "block"),
    )
