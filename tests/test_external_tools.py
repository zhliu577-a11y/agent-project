import json
from pathlib import Path

import pytest

from core.errors import DeclaredPluginError, ToolError
from core.registry import ToolRegistry
from plugins.loader import inspect_package, load_tool_plugins

_SERVER = r"""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    request_id = request["id"]
    params = request.get("params", {})
    name = params.get("name")
    arguments = params.get("arguments", {})
    text = arguments.get("text", "")

    if text == "declared":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": "invalid_text",
                "message": "text was rejected",
            },
        }
    elif text == "tool-error":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "remote tool failed"}],
            },
        }
    else:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": f"{name}:{text}"}],
            },
        }
    print(json.dumps(response, ensure_ascii=False), flush=True)
"""


def _write_external_plugin(
    root: Path,
    *,
    protocol: str = "jsonrpc-stdio",
    include_tools: bool = True,
) -> Path:
    plugin_dir = root / "tools" / "external"
    plugin_dir.mkdir(parents=True)
    entry = {
        "runtime": "process",
        "protocol": protocol,
        "command": "python",
        "args": ["server.py"],
        "timeout": 5,
    }
    if include_tools:
        entry["tools"] = [
            {
                "name": "echo",
                "description": "Echo text through an external process.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]
    manifest = {
        "name": "external",
        "type": "tool",
        "version": "1.0.0",
        "errors": [
            {
                "code": "invalid_text",
                "category": "tool",
                "hint": "change the input text",
            }
        ],
        "entry": entry,
    }
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    (plugin_dir / "server.py").write_text(_SERVER, encoding="utf-8")
    return plugin_dir


@pytest.mark.asyncio
async def test_external_stdio_tool_loads_executes_and_restarts(tmp_path) -> None:
    _write_external_plugin(tmp_path)
    manifests = load_tool_plugins(tmp_path)

    assert len(manifests) == 1
    manifest, tools = manifests[0]
    assert manifest.name == "external"
    assert [tool.name for tool in tools] == ["external__echo"]

    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    assert await registry.execute("external__echo", {"text": "hello"}) == "echo:hello"
    await tools[0].close()
    assert await registry.execute("external__echo", {"text": "again"}) == "echo:again"
    await tools[0].close()


@pytest.mark.asyncio
async def test_external_stdio_tool_maps_declared_and_generic_errors(tmp_path) -> None:
    _write_external_plugin(tmp_path)
    _, tools = load_tool_plugins(tmp_path)[0]
    registry = ToolRegistry()
    registry.register(tools[0])

    with pytest.raises(DeclaredPluginError) as info:
        await registry.execute("external__echo", {"text": "declared"})
    assert info.value.code == "invalid_text"
    assert info.value.category == "tool"
    assert info.value.hint == "change the input text"

    with pytest.raises(ToolError, match="remote tool failed"):
        await registry.execute("external__echo", {"text": "tool-error"})
    await tools[0].close()


def test_external_stdio_tool_rejects_invalid_protocol(tmp_path) -> None:
    _write_external_plugin(tmp_path, protocol="json-lines")
    with pytest.raises(ValueError, match="jsonrpc-stdio"):
        load_tool_plugins(tmp_path)


def test_external_stdio_tool_requires_tool_catalog(tmp_path) -> None:
    _write_external_plugin(tmp_path, include_tools=False)
    with pytest.raises(ValueError, match="entry.tools"):
        load_tool_plugins(tmp_path)


def test_external_stdio_tool_passes_static_inspection(tmp_path) -> None:
    plugin_dir = _write_external_plugin(tmp_path)
    inspection = inspect_package(plugin_dir)

    assert inspection.name == "external"
    assert inspection.manifests[0].entry["runtime"] == "process"


def test_external_runtime_is_reserved_for_tool_kind(tmp_path) -> None:
    plugin_dir = tmp_path / "context" / "invalid"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(
            {
                "name": "invalid",
                "type": "context",
                "entry": {
                    "runtime": "process",
                    "module": "policy.py",
                    "factory": "create_policy",
                },
            }
        ),
        encoding="utf-8",
    )
    (plugin_dir / "policy.py").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="只支持 runtime 'python'"):
        inspect_package(plugin_dir)
