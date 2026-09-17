# tests/test_listeners.py —— listener 插件：订阅总线、声明校验、退订
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.events import Event, EventBus, EventIdentity
from plugins.loader import attach_listener_plugins, load_listener_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"

_LISTENER_MODULE = """
import json
from pathlib import Path

from core.events import Subscription


def create_listener(plugin_dir):
    log_path = Path(plugin_dir) / "seen.jsonl"

    def handler(event):
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps({"name": event.name, "trace": event.trace_id}) + "\\n")

    return [Subscription(event="tool.after", handler=handler, priority=5)]
"""


def _write_listener(
    tmp_path: Path,
    name: str = "demo",
    events: list[str] | None = None,
    module: str = _LISTENER_MODULE,
) -> Path:
    plugin_dir = tmp_path / "listeners" / name
    plugin_dir.mkdir(parents=True)
    manifest: dict = {
        "name": name,
        "type": "listener",
        "entry": {"module": "listener.py", "factory": "create_listener"},
    }
    if events is not None:
        manifest["events"] = events
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (plugin_dir / "listener.py").write_text(module, encoding="utf-8")
    return plugin_dir


def test_load_listener_plugins_returns_subscriptions(tmp_path) -> None:
    _write_listener(tmp_path, events=["tool.after"])
    plugins = load_listener_plugins(tmp_path)
    assert len(plugins) == 1
    subscriptions = plugins[0].create()
    assert subscriptions[0].event == "tool.after"
    assert subscriptions[0].priority == 5
    assert callable(subscriptions[0].handler)


def test_listener_declared_events_are_enforced(tmp_path) -> None:
    _write_listener(tmp_path, events=["memory.write"])
    plugin = load_listener_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="未声明的事件"):
        plugin.create()


def test_listener_subscription_names_still_require_manifest_declaration(tmp_path) -> None:
    module = """
from core.events import Subscription


def create_listener(plugin_dir):
    return [Subscription(event="user_prompt.accepted", handler=lambda event: None)]
"""
    _write_listener(tmp_path, events=["memory.write"], module=module)
    plugin = load_listener_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="未声明的事件"):
        plugin.create()


def test_listener_factory_bad_return_fails_fast(tmp_path) -> None:
    _write_listener(tmp_path, module="def create_listener(plugin_dir):\n    return 42\n")
    plugin = load_listener_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="Subscription"):
        plugin.create()


def test_listener_factory_can_receive_its_scoped_capability_view(tmp_path) -> None:
    module = """
from core.events import Subscription


def create_listener(plugin_dir, subscriber=None):
    assert subscriber is not None
    assert subscriber.identity.subject == "host:test-listener"
    return [Subscription(event="demo", handler=lambda event: None)]
"""
    _write_listener(tmp_path, module=module)
    plugin = load_listener_plugins(tmp_path)[0]
    gateway = EventBus()
    subscriber = gateway.subscriber(EventIdentity.host("test-listener"))

    subscriptions = plugin.create(subscriber)

    assert subscriptions[0].event == "demo"


def test_manifest_rejects_bad_events_field(tmp_path) -> None:
    plugin_dir = _write_listener(tmp_path)
    (plugin_dir / "plugin.json").write_text(
        '{"name": "demo", "type": "listener", "events": "tool.after",'
        ' "entry": {"module": "listener.py", "factory": "create_listener"}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="events"):
        load_listener_plugins(tmp_path)


@pytest.mark.asyncio
async def test_attach_listener_plugins_registers_and_unsubscribes(tmp_path) -> None:
    plugin_dir = _write_listener(tmp_path, events=["tool.after"])
    plugin = load_listener_plugins(tmp_path)[0]
    bus = EventBus()

    tokens = attach_listener_plugins(bus, [plugin])
    assert bus.subscriber_count("tool.after") == 1
    subscription = bus.subscriptions()[0]
    assert subscription.owner.subject == "plugin:listener:demo"

    await bus.publish(Event("tool.after", {"ok": True}))
    await bus.flush()
    log_path = plugin_dir / "seen.jsonl"
    assert json.loads(log_path.read_text(encoding="utf-8").strip())["name"] == "tool.after"

    tokens[0]()  # 退订
    await bus.publish(Event("tool.after", {"ok": True}))
    await bus.flush()
    assert len(log_path.read_text(encoding="utf-8").strip().splitlines()) == 1
    await bus.stop()


def test_repo_timeline_listener_is_discoverable() -> None:
    plugins = load_listener_plugins(REPO_PLUGINS)
    by_name = {plugin.manifest.name: plugin for plugin in plugins}
    assert "timeline" in by_name
    subscriptions = by_name["timeline"].create()
    assert subscriptions[0].event == "*"


def test_repo_tool_metrics_listener_is_discoverable() -> None:
    plugins = load_listener_plugins(REPO_PLUGINS)
    by_name = {plugin.manifest.name: plugin for plugin in plugins}

    assert "tool-metrics" in by_name
    subscriptions = by_name["tool-metrics"].create()
    assert {subscription.event for subscription in subscriptions} == {
        "tool.after",
        "tool.denied",
    }


def test_tool_metrics_listener_writes_to_data_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOOL_METRICS_PATH", raising=False)
    plugin = next(
        item for item in load_listener_plugins(REPO_PLUGINS) if item.manifest.name == "tool-metrics"
    )
    handlers = {subscription.event: subscription.handler for subscription in plugin.create()}
    tool_call = SimpleNamespace(name="demo", id="call-1")

    handlers["tool.after"](Event("tool.after", {"tool_call": tool_call, "ok": True}))

    output_path = tmp_path / "data" / "tool-metrics.json"
    assert json.loads(output_path.read_text(encoding="utf-8"))["byTool"]["demo"]["ok"] == 1
    assert not (plugin.manifest.directory / "tool-metrics.json").exists()


def test_tool_metrics_listener_supports_output_path_override(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "runtime" / "metrics.json"
    monkeypatch.setenv("TOOL_METRICS_PATH", str(output_path))
    plugin = next(
        item for item in load_listener_plugins(REPO_PLUGINS) if item.manifest.name == "tool-metrics"
    )
    handlers = {subscription.event: subscription.handler for subscription in plugin.create()}
    tool_call = SimpleNamespace(name="demo", id="call-1")

    handlers["tool.denied"](Event("tool.denied", {"tool_call": tool_call}))

    assert json.loads(output_path.read_text(encoding="utf-8"))["byTool"]["demo"]["denied"] == 1
