from dataclasses import replace
from pathlib import Path

import pytest

from core.errors import ToolError
from core.registry import ToolRegistry
from gateways.mcp_gateway import McpGateway, UsePlugin
from plugins.loader import load_mcp_plugins


@pytest.mark.asyncio
async def test_filesystem_mcp_reads_writes_and_rejects_escape(tmp_path, monkeypatch) -> None:
    plugin_root = Path(__file__).resolve().parents[1] / "plugins"
    spec = next(
        item for item in load_mcp_plugins(plugin_root) if item.manifest.name == "filesystem"
    )
    workspace = tmp_path / "workspace"
    spec = replace(spec, args=[spec.args[0], str(workspace)])
    monkeypatch.setenv("HARNESS_FILESYSTEM_ROOT", str(tmp_path / "ignored"))

    gateway = McpGateway([spec], connect_timeout=10, call_timeout=10)
    registry = ToolRegistry()
    use_plugin = UsePlugin(gateway, registry)
    try:
        assert gateway.status()["filesystem"] == "idle"
        assert registry.describe("filesystem__write_file") is None

        mounted, message = await use_plugin.mount("filesystem")
        assert mounted is True
        assert "filesystem__write_file" in message
        assert gateway.status()["filesystem"] == "loaded"
        assert registry.describe("filesystem__write_file") is not None
        assert registry.describe("filesystem__read_text_file") is not None
        assert registry.describe("filesystem__list_directory") is not None

        written = await registry.execute(
            "filesystem__write_file",
            {"path": "notes/example.txt", "content": "hello from mcp"},
        )
        assert "Wrote" in written

        read = await registry.execute(
            "filesystem__read_text_file", {"path": "notes/example.txt"}
        )
        assert read == "hello from mcp"

        listing = await registry.execute("filesystem__list_directory", {"path": "notes"})
        assert "[FILE] example.txt" in listing

        roots = await registry.execute("filesystem__list_allowed_directories", {})
        assert str(workspace.resolve()) in roots

        with pytest.raises(ToolError, match="path_outside_allowed_roots"):
            await registry.execute(
                "filesystem__read_text_file", {"path": "../escape.txt"}
            )
    finally:
        await gateway.close()
