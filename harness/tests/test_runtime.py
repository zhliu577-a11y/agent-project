import json
from dataclasses import replace
from pathlib import Path

import pytest

from config import AppConfig
from plugins.manager import PluginManager
from runtime import HarnessRuntime, RuntimeStartupError

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

_MEMORY_EXTRACTOR_MODULE = """
from core.memory_extraction import MemoryExtractionResult


class FakeMemoryExtractor:
    async def extract(self, request):
        return MemoryExtractionResult()


def create_extractor(plugin_dir, context=None):
    return FakeMemoryExtractor()
"""

_MEMORY_RETRIEVER_MODULE = """
from core.memory import StoreMemoryRetriever


def create_retriever(plugin_dir, memory):
    return StoreMemoryRetriever(memory)
"""

_CONTEXT_MODULE = """
from core.context import TailWindowPolicy


def create_policy(plugin_dir):
    return TailWindowPolicy()
"""

_EVENT_TRANSPORT_MODULE = """
from core.events import InProcessTransport


def create_transport(plugin_dir):
    return InProcessTransport()
"""

_MODEL_ROUTER_MODULE = """
from core.model import ModelRouteDecision, ModelRouter


class StaticRouter(ModelRouter):
    def route(self, request):
        candidates = request.routes.get(request.role, (request.default_model,))
        return ModelRouteDecision(candidates=tuple(candidates) + request.fallback)


def create_router(plugin_dir):
    return StaticRouter()
"""

_MCP_SERVER = """
from mcp.server.mcpserver import MCPServer


server = MCPServer(name="demo-server")


@server.tool()
def echo(text: str) -> str:
    \"\"\"Echo text through MCP.\"\"\"
    return f"mcp:{text}"


if __name__ == "__main__":
    server.run()
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
        "event_transports",
        "in-process",
        {
            "name": "in-process",
            "type": "event-transport",
            "entry": {"module": "transport.py", "factory": "create_transport"},
        },
        {"transport.py": _EVENT_TRANSPORT_MODULE},
    )
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
        "memory-extractor",
        "explicit",
        {
            "name": "explicit",
            "type": "memory-extractor",
            "entry": {"module": "extractor.py", "factory": "create_extractor"},
        },
        {"extractor.py": _MEMORY_EXTRACTOR_MODULE},
    )
    _write_builtin(
        root,
        "memory-retriever",
        "store-native",
        {
            "name": "store-native",
            "type": "memory-retriever",
            "requires": [{"kind": "memory", "inject": "memory"}],
            "entry": {"module": "retriever.py", "factory": "create_retriever"},
        },
        {"retriever.py": _MEMORY_RETRIEVER_MODULE},
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
        "model_routers",
        "static",
        {
            "name": "static",
            "type": "model-router",
            "entry": {"module": "router.py", "factory": "create_router"},
        },
        {"router.py": _MODEL_ROUTER_MODULE},
    )
    _write_builtin(
        root,
        "mcp",
        "demo",
        {
            "name": "demo",
            "type": "mcp",
            "description": "Demo MCP server.",
            "entry": {"command": "python", "args": ["server.py"]},
        },
        {"server.py": _MCP_SERVER},
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


def _config(
    *,
    mcp_preload: tuple[str, ...] = (),
    skill_preload: tuple[str, ...] = (),
    skill_max_preload_bytes: int = 524288,
    skill_permissions: tuple[tuple[str, str], ...] = (),
    model_router: str | None = None,
) -> AppConfig:
    return AppConfig(
        model="fake",
        session_store="memory",
        session_id="test",
        memory_store="memory",
        embedding_provider="debug",
        context_max_tokens=20000,
        context_strategy="tail-window",
        mcp_preload=mcp_preload,
        skill_preload=skill_preload,
        skill_max_preload_bytes=skill_max_preload_bytes,
        skill_permissions=skill_permissions,
        model_router=model_router,
    )


def _write_skill_package(root: Path, *, legacy_preload: bool = False) -> Path:
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
                    "contract": "skill.v1",
                    "entry": {
                        "content": "SKILL.md",
                        "preload": legacy_preload,
                    },
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
        skill_status = next(status for status in snapshot.skills if status.name == "quality--lint")
        assert skill_status.status == "ready"
        assert skill_status.loaded is False
        assert skill_status.access == "allow"
        assert skill_status.priority == 0
        assert skill_status.listing == "full"
        assert skill_status.usage.requests == 0
        assert "external__echo" in snapshot.tools
        assert type(runtime.context_policy).__name__ == "TailWindowPolicy"
        assert runtime.context_gateway is not None
        assert runtime.services.require("memory", "memory") is runtime.memory_store
        assert manager.list_installed()[0].runtime_status == "active"

    assert manager.list_installed()[0].runtime_status == "stopped"


@pytest.mark.asyncio
async def test_runtime_hides_denied_skills_from_prompt_and_tool(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    manager.install(_write_skill_package(tmp_path / "source"))
    manager.enable("quality")
    runtime = HarnessRuntime(
        _config(skill_permissions=(("*", "deny"),)),
        plugin_manager=manager,
    )

    async with runtime:
        assert runtime.skills is not None
        assert runtime.skills.available() == []
        assert runtime.snapshot().skills == ()
        assert "quality--lint" not in runtime.system_prompt
        use_skill = next(
            schema
            for schema in runtime.tools.list_schemas()
            if schema["function"]["name"] == "use_skill"
        )
        assert (
            "quality--lint"
            not in use_skill["function"]["parameters"]["properties"]["name"]["description"]
        )


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_event_transport(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    runtime = HarnessRuntime(
        replace(_config(), event_transport="missing"),
        plugin_manager=_manager(tmp_path),
    )

    with pytest.raises(RuntimeStartupError, match="unknown event transport plugin"):
        await runtime.start()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_preloads_only_host_selected_skills(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("EVENT_LOG", str(event_log))
    manager = _manager(tmp_path)
    manager.install(_write_skill_package(tmp_path / "source", legacy_preload=True))
    manager.enable("quality")
    runtime = HarnessRuntime(
        _config(skill_preload=("quality--lint",)),
        plugin_manager=manager,
    )

    async with runtime:
        snapshot = runtime.snapshot()
        status = next(item for item in snapshot.skills if item.name == "quality--lint")

        assert status.status == "loaded"
        assert status.preload is True
        assert status.loaded is True
        assert status.bytes > 0
        assert "# lint" in runtime.system_prompt
        records = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
        assert [record["name"] for record in records] == [
            "session.loaded",
            "skill.preloaded",
        ]


@pytest.mark.asyncio
async def test_runtime_ignores_legacy_manifest_preload_without_host_selection(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    manager.install(_write_skill_package(tmp_path / "source", legacy_preload=True))
    manager.enable("quality")
    runtime = HarnessRuntime(_config(), plugin_manager=manager)

    async with runtime:
        status = next(item for item in runtime.snapshot().skills if item.name == "quality--lint")
        assert status.preload is False
        assert status.loaded is False
        assert "# lint" not in runtime.system_prompt


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_skill_preload(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    runtime = HarnessRuntime(
        _config(skill_preload=("missing",)),
        plugin_manager=_manager(tmp_path),
    )

    with pytest.raises(RuntimeStartupError, match="未知的 Skill 预加载插件"):
        await runtime.start()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_enforces_skill_preload_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    manager.install(_write_skill_package(tmp_path / "source"))
    manager.enable("quality")
    runtime = HarnessRuntime(
        _config(
            skill_preload=("quality--lint",),
            skill_max_preload_bytes=3,
        ),
        plugin_manager=manager,
    )

    with pytest.raises(RuntimeStartupError, match="预加载总大小超过预算"):
        await runtime.start()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_executes_and_closes_external_tool(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    runtime = HarnessRuntime(_config(), plugin_manager=manager)

    async with runtime:
        assert await runtime.tools.execute("external__echo", {"text": "hello"}) == "external:hello"


@pytest.mark.asyncio
async def test_runtime_owns_model_gateway_with_selected_router(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    runtime = HarnessRuntime(
        _config(model_router="static"),
        plugin_manager=manager,
    )

    async with runtime:
        assert runtime.model is runtime.models
        status = next(item for item in runtime.snapshot().models if item.name == "fake")
        assert status.status == "active"
        assert status.active is True


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


@pytest.mark.asyncio
async def test_runtime_preloads_selected_mcp_before_first_model_call(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    manager = _manager(tmp_path)
    runtime = HarnessRuntime(_config(mcp_preload=("demo",)), plugin_manager=manager)

    async with runtime:
        snapshot = runtime.snapshot()
        assert snapshot.ready is True
        assert "use_plugin" in snapshot.tools
        assert "demo__echo" in snapshot.tools
        assert await runtime.tools.execute("demo__echo", {"text": "hello"}) == "mcp:hello"
        assert next(status for status in snapshot.mcp if status.name == "demo").status == "loaded"


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_mcp_preload(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)
    runtime = HarnessRuntime(_config(mcp_preload=("missing",)), plugin_manager=_manager(tmp_path))

    with pytest.raises(RuntimeStartupError, match="未知的 MCP 预加载插件"):
        await runtime.start()
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_continues_when_mcp_preload_connection_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("EVENT_LOG", raising=False)

    async def fail_mount(self, name):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("gateways.mcp_gateway.McpGateway.mount", fail_mount)
    manager = _manager(tmp_path)
    runtime = HarnessRuntime(_config(mcp_preload=("demo",)), plugin_manager=manager)

    async with runtime:
        snapshot = runtime.snapshot()
        assert snapshot.ready is True
        assert "demo__echo" not in snapshot.tools
        assert "use_plugin" in snapshot.tools
