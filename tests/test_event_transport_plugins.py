# tests/test_event_transport_plugins.py - event transport plugin contract
import json
from pathlib import Path

import pytest

from config import AppConfig
from core.events import Event, EventGateway, InProcessTransport
from plugins.loader import load_event_transport_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _write_transport(tmp_path: Path, module: str) -> Path:
    plugin_dir = tmp_path / "event_transports" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "type": "event-transport",
                "entry": {"module": "transport.py", "factory": "create_transport"},
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "transport.py").write_text(module, encoding="utf-8")
    return plugin_dir


def test_event_transport_plugin_loads_lazily_and_creates_transport(tmp_path) -> None:
    _write_transport(
        tmp_path,
        "from core.events import InProcessTransport\n\n"
        "def create_transport(plugin_dir):\n"
        "    return InProcessTransport(queue_size=8)\n",
    )

    plugins = load_event_transport_plugins(tmp_path)

    assert len(plugins) == 1
    transport = plugins[0].create()
    assert isinstance(transport, InProcessTransport)
    assert transport.queue_size == 8
    assert isinstance(EventGateway(transport=transport), EventGateway)


def test_event_transport_plugin_rejects_wrong_factory_result(tmp_path) -> None:
    _write_transport(
        tmp_path,
        "def create_transport(plugin_dir):\n    return object()\n",
    )

    plugin = load_event_transport_plugins(tmp_path)[0]

    with pytest.raises(ValueError, match="EventTransport"):
        plugin.create()


def test_event_transport_plugin_receives_private_config(tmp_path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    plugin_config = config_dir / "plugins" / "event-transport"
    plugin_config.mkdir(parents=True)
    (plugin_config / "demo.json").write_text(
        json.dumps({"queueSize": 7}),
        encoding="utf-8",
    )
    _write_transport(
        tmp_path,
        "from core.events import InProcessTransport\n\n"
        "def create_transport(plugin_dir, context=None):\n"
        "    return InProcessTransport(queue_size=context.config['queueSize'])\n",
    )

    plugin = load_event_transport_plugins(tmp_path, AppConfig.load(config_dir))[0]

    assert plugin.create().queue_size == 7


@pytest.mark.asyncio
async def test_repo_inline_transport_delivers_immediately() -> None:
    plugins = load_event_transport_plugins(REPO_PLUGINS)
    by_name = {plugin.manifest.name: plugin for plugin in plugins}
    gateway = EventGateway(transport=by_name["inline"].create())
    seen: list[str] = []
    gateway.subscribe("demo", lambda event: seen.append(event.name))

    await gateway.start()
    await gateway.publish(Event("demo"))

    assert seen == ["demo"]
    await gateway.stop()
