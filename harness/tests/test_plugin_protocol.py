import json
from pathlib import Path

import pytest

from plugins import loader as plugin_loader
from plugins.context import PluginContext
from plugins.lifecycle import PluginLifecycleManager
from plugins.loader import KindHandler, discover_plugins, load_hook_plugins, register_kind
from plugins.protocol import MANIFEST_API_VERSION, PLUGIN_PROTOCOL_VERSION

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_hook(root: Path, name: str, **extra) -> Path:
    plugin_dir = root / "hooks" / name
    plugin_dir.mkdir(parents=True)
    manifest = {
        "name": name,
        "type": "hook",
        "version": "1.0.0",
        "entry": {"module": "hook.py", "factory": "create_hook"},
        **extra,
    }
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "hook.py").write_text(
        "from core.hooks import LifecycleHooks\n"
        "def create_hook(plugin_dir):\n"
        "    return LifecycleHooks()\n",
        encoding="utf-8",
    )
    return plugin_dir


def test_manifest_records_api_protocol_and_capability_contract(tmp_path) -> None:
    _write_hook(
        tmp_path,
        "recorder",
        apiVersion=MANIFEST_API_VERSION,
        protocolVersion=PLUGIN_PROTOCOL_VERSION,
        contract="hook.v1",
    )

    manifest = discover_plugins(tmp_path)[0]

    assert manifest.api_version == "1"
    assert manifest.protocol_version == 1
    assert manifest.contract == "hook.v1"


def test_manifest_rejects_unsupported_protocol_version(tmp_path) -> None:
    _write_hook(tmp_path, "future", protocolVersion=999)

    with pytest.raises(ValueError, match="protocolVersion"):
        discover_plugins(tmp_path)


def test_manifest_rejects_contract_version_mismatch(tmp_path) -> None:
    _write_hook(tmp_path, "wrong-contract", contract="hook.v2")

    with pytest.raises(ValueError, match="contract mismatch"):
        discover_plugins(tmp_path)


def test_kind_negotiates_multiple_protocol_versions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_loader, "_KIND_REGISTRY", dict(plugin_loader._KIND_REGISTRY))
    monkeypatch.setattr(plugin_loader, "SUPPORTED_KINDS", plugin_loader.SUPPORTED_KINDS)
    register_kind(
        KindHandler(
            "multi",
            lambda manifest: manifest.name,
            lambda _assembly, _manifest, _payload: None,
            protocol_version=2,
            protocol_versions=(1, 2),
            explicit_contract=True,
        ),
        replace=True,
    )
    plugin_dir = tmp_path / "multi" / "legacy"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "legacy",
                "type": "multi",
                "protocolVersion": 1,
                "contract": "multi.v1",
                "entry": {},
            }
        ),
        encoding="utf-8",
    )

    manifest = discover_plugins(tmp_path)[0]

    assert manifest.protocol_version == 1
    assert manifest.contract == "multi.v1"


def test_kind_uses_preferred_protocol_version_when_omitted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_loader, "_KIND_REGISTRY", dict(plugin_loader._KIND_REGISTRY))
    monkeypatch.setattr(plugin_loader, "SUPPORTED_KINDS", plugin_loader.SUPPORTED_KINDS)
    register_kind(
        KindHandler(
            "multi-default",
            lambda manifest: manifest.name,
            lambda _assembly, _manifest, _payload: None,
            protocol_version=2,
            protocol_versions=(1, 2),
            explicit_contract=True,
        ),
        replace=True,
    )
    plugin_dir = tmp_path / "multi-default" / "current"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "current",
                "type": "multi-default",
                "contract": "multi-default.v2",
                "entry": {},
            }
        ),
        encoding="utf-8",
    )

    manifest = discover_plugins(tmp_path)[0]

    assert manifest.protocol_version == 2
    assert manifest.contract == "multi-default.v2"


def test_builtin_manifests_declare_the_platform_protocol_explicitly() -> None:
    manifests = list((PROJECT_ROOT / "plugins").glob("**/plugin.json"))

    assert manifests
    for path in manifests:
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["apiVersion"] == MANIFEST_API_VERSION
        assert raw["protocolVersion"] == PLUGIN_PROTOCOL_VERSION
        if "contributes" in raw:
            for contribution in raw["contributes"]:
                assert (
                    contribution["contract"] == f"{contribution['kind']}.v{PLUGIN_PROTOCOL_VERSION}"
                )
        else:
            assert raw["contract"] == f"{raw['type']}.v{PLUGIN_PROTOCOL_VERSION}"


class LifecyclePlugin:
    def __init__(self, trace: list[str], name: str) -> None:
        self._trace = trace
        self._name = name

    async def setup(self, context: PluginContext) -> None:
        self._trace.append(f"setup:{self._name}:{context.contract}")

    async def start(self) -> None:
        self._trace.append(f"start:{self._name}")

    async def stop(self) -> None:
        self._trace.append(f"stop:{self._name}")


@pytest.mark.asyncio
async def test_lifecycle_runs_setup_start_and_reverse_stop() -> None:
    trace: list[str] = []
    manager = PluginLifecycleManager()
    for name in ("a", "b"):
        manager.add(
            PluginContext.create(
                name=name,
                kind="hook",
                directory=Path("."),
                contract="hook.v1",
            ),
            LifecyclePlugin(trace, name),
        )

    await manager.start_all()
    await manager.stop_all()

    assert trace == [
        "setup:a:hook.v1",
        "start:a",
        "setup:b:hook.v1",
        "start:b",
        "stop:b",
        "stop:a",
    ]


@pytest.mark.asyncio
async def test_lifecycle_rolls_back_already_started_plugins() -> None:
    trace: list[str] = []

    class Broken(LifecyclePlugin):
        async def start(self) -> None:
            self._trace.append(f"start:{self._name}")
            raise RuntimeError("boom")

    manager = PluginLifecycleManager()
    manager.add(
        PluginContext.create(name="a", kind="hook", directory=Path(".")),
        LifecyclePlugin(trace, "a"),
    )
    manager.add(
        PluginContext.create(name="b", kind="hook", directory=Path(".")),
        Broken(trace, "b"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await manager.start_all()

    assert trace[-2:] == ["stop:b", "stop:a"]


@pytest.mark.asyncio
async def test_lifecycle_rolls_back_plugin_whose_setup_fails() -> None:
    trace: list[str] = []

    class BrokenSetup(LifecyclePlugin):
        async def setup(self, context: PluginContext) -> None:
            self._trace.append(f"setup:{self._name}:{context.contract}")
            raise RuntimeError("setup boom")

    manager = PluginLifecycleManager()
    manager.add(
        PluginContext.create(
            name="a",
            kind="hook",
            directory=Path("."),
            contract="hook.v1",
        ),
        LifecyclePlugin(trace, "a"),
    )
    manager.add(
        PluginContext.create(
            name="b",
            kind="hook",
            directory=Path("."),
            contract="hook.v1",
        ),
        BrokenSetup(trace, "b"),
    )

    with pytest.raises(RuntimeError, match="setup boom"):
        await manager.start_all()

    assert trace[-3:] == ["setup:b:hook.v1", "stop:b", "stop:a"]


def test_loader_passes_protocol_context_to_new_factory(tmp_path) -> None:
    plugin_dir = tmp_path / "hooks" / "context-aware"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "context-aware",
                "type": "hook",
                "protocolVersion": 1,
                "contract": "hook.v1",
                "entry": {"module": "hook.py", "factory": "create_hook"},
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "hook.py").write_text(
        "from core.hooks import LifecycleHooks\n"
        "class H(LifecycleHooks):\n"
        "    def __init__(self, context):\n"
        "        self.context = context\n"
        "def create_hook(plugin_dir, context=None):\n"
        "    return H(context)\n",
        encoding="utf-8",
    )

    manifest, hook = load_hook_plugins(tmp_path)[0]

    assert hook.context.contract == "hook.v1"
    assert hook.context.protocol_version == manifest.protocol_version
