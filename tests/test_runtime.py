import json
from pathlib import Path

import pytest

from core.config import AppConfig
from plugins.manager import PluginManager
from runtime import HarnessRuntime

_MODEL_MODULE = """
from core.model import ModelAdapter
from core.types import ModelResponse


class FakeModel(ModelAdapter):
    async def complete(self, messages, tool_schemas, on_token=None):
        return ModelResponse(content="ok", tool_calls=[])


def create_model(plugin_dir):
    return FakeModel()
"""

_SESSION_MODULE = """
from core.session import SessionStore


class FakeStore(SessionStore):
    async def load(self, session_id):
        return []

    async def save(self, session_id, messages):
        pass

    async def load_checkpoint(self, session_id):
        return None

    async def save_checkpoint(self, session_id, snapshot):
        pass

    async def delete_checkpoint(self, session_id):
        pass


def create_store(plugin_dir):
    return FakeStore()
"""

_MEMORY_MODULE = """
from core.memory import MemoryStore


class FakeMemory(MemoryStore):
    async def list_notes(self):
        return []

    async def add_note(self, content, tags):
        return None

    async def delete_note(self, note_id):
        return False

    async def update_note(self, note_id, content=None, tags=None):
        return None

    async def search_notes(self, query):
        return []


def create_store(plugin_dir):
    return FakeMemory()
"""

_CONTEXT_MODULE = """
from core.context import TailWindowPolicy


def create_policy(plugin_dir):
    return TailWindowPolicy()
"""

_EXTERNAL_TOOL_SERVER = r"""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    params = request.get("params", {})
    text = params.get("arguments", {}).get("text", "")
    result = {"content": [{"type": "text", "text": f"external:{text}"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_builtin(
    root: Path,
    kind: str,
    name: str,
    manifest: dict,
    files: dict[str, str],
) -> Path:
    plugin_dir = root / kind / name
    _write_json(plugin_dir / "plugin.json", manifest)
    for relative, content in files.items():
        path = plugin_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return plugin_dir


def _write_builtins(root: Path) -> None:
    _write_builtin(
        root,
        "model",
        "fake",
        {
            "name": "fake",
            "type": "model",
            "entry": {"module": "model.py", "factory": "create_model"},
        },
        {"model.py": _MODEL_MODULE},
    )
    _write_builtin(
        root,
        "session",
        "memory",
        {
            "name": "memory",
            "type": "session",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        {"store.py": _SESSION_MODULE},
    )
    _write_builtin(
        root,
        "memory",
        "memory",
        {
            "name": "memory",
            "type": "memory",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        {"store.py": _MEMORY_MODULE},
    )
    _write_builtin(
        root,
        "context",
        "tail-window",
        {
            "name": "tail-window",
            "type": "context",
            "entry": {"module": "policy.py", "factory": "create_policy"},
        },
        {"policy.py": _CONTEXT_MODULE},
    )
    _write_builtin(
        root,
        "tools",
        "external",
        {
            "name": "external",
            "type": "tool",
            "entry": {
                "runtime": "process",
                "protocol": "jsonrpc-stdio",
                "command": "python",
                "args": ["server.py"],
                "tools": [
                    {
                        "name": "echo",
                        "description": "External echo.",
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    }
                ],
            },
        },
        {"server.py": _EXTERNAL_TOOL_SERVER},
    )


def _manager(tmp_path: Path) -> PluginManager:
    builtins = tmp_path / "builtins"
    _write_builtins(builtins)
    return PluginManager(
        registry_path=tmp_path / "data" / "plugin-registry.json",
        store_dir=tmp_path / "data" / "plugin-store",
        builtin_dir=builtins,
    )


def _config() -> AppConfig:
    return AppConfig(
        model="fake",
        session_store="memory",
        session_id="test",
        memory_store="memory",
        embedding_provider="debug",
        context_max_tokens=20000,
        context_strategy="tail-window",
    )


def _write_skill_package(root: Path) -> Path:
    package = root / "quality"
    _write_json(
        package / "plugin.json",
        {
            "apiVersion": "1",
            "name": "quality",
            "version": "1.0.0",
            "contributes": [
                {
                    "id": "lint",
                    "kind": "skill",
                    "entry": {"content": "SKILL.md"},
                }
            ],
        },
    )
    (package / "SKILL.md").write_text("# lint\n", encoding="utf-8")
    return package


def _write_broken_tool_package(root: Path) -> Path:
    package = root / "broken"
    _write_json(
        package / "plugin.json",
        {
            "name": "broken",
            "type": "tool",
            "version": "1.0.0",
            "entry": {"module": "tool.py", "factory": "create_tools"},
        },
    )
    (package / "tool.py").write_text(
        "def create_tools(plugin_dir):\n    return 42\n",
        encoding="utf-8",
    )
    return package


@pytest.mark.asyncio
async def test_runtime_loads_enabled_package_and_records_status(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    manager.install(_write_skill_package(tmp_path / "source"))
    manager.enable("quality")

    runtime = HarnessRuntime(_config(), plugin_manager=manager)
    async with runtime:
        snapshot = runtime.snapshot()
        states = {status.name: status for status in snapshot.plugins}

        assert snapshot.ready is True
        assert states["quality"].status == "active"
        assert states["quality"].contributions == ("skill:quality--lint",)
        assert "use_skill" in snapshot.tools
        assert "external__echo" in snapshot.tools
        assert type(runtime.context_policy).__name__ == "TailWindowPolicy"
        assert manager.list_installed()[0].runtime_status == "active"

    assert manager.list_installed()[0].runtime_status == "stopped"


@pytest.mark.asyncio
async def test_runtime_executes_and_closes_external_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    runtime = HarnessRuntime(_config(), plugin_manager=manager)

    async with runtime:
        assert await runtime.tools.execute("external__echo", {"text": "hello"}) == "external:hello"


@pytest.mark.asyncio
async def test_runtime_isolates_failed_external_package(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    manager.install(_write_broken_tool_package(tmp_path / "source"))
    manager.enable("broken")

    runtime = HarnessRuntime(_config(), plugin_manager=manager)
    async with runtime:
        snapshot = runtime.snapshot()
        state = next(status for status in snapshot.plugins if status.name == "broken")
        record = manager.list_installed()[0]

        assert snapshot.ready is True
        assert state.status == "error"
        assert "Tool" in (state.error or "")
        assert record.runtime_status == "error"
        assert "Tool" in (record.last_error or "")
