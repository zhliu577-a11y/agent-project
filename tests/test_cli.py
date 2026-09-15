import json
from pathlib import Path

from cli import main


def _write_package(root: Path) -> Path:
    package = root / "quality"
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(
        json.dumps(
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
            }
        ),
        encoding="utf-8",
    )
    (package / "SKILL.md").write_text("# lint\n", encoding="utf-8")
    return package


def _write_mcp_builtin(root: Path) -> None:
    package = root / "mcp" / "demo"
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(
        json.dumps(
            {
                "name": "demo",
                "type": "mcp",
                "entry": {"command": "python", "args": ["server.py"]},
            }
        ),
        encoding="utf-8",
    )
    (package / "server.py").write_text("# placeholder\n", encoding="utf-8")


def test_cli_install_enable_and_list(tmp_path, capsys) -> None:
    package = _write_package(tmp_path / "source")
    common = [
        "--registry",
        str(tmp_path / "data" / "plugin-registry.json"),
        "--store-dir",
        str(tmp_path / "data" / "plugin-store"),
        "--plugin-dir",
        str(tmp_path / "builtins"),
    ]

    assert main([*common, "plugin", "install", str(package)]) == 0
    assert "installed quality 1.0.0 (disabled)" in capsys.readouterr().out

    assert main([*common, "plugin", "enable", "quality"]) == 0
    assert "enabled quality" in capsys.readouterr().out

    assert main([*common, "plugin", "list"]) == 0
    assert "quality\t1.0.0\tenabled\tidle" in capsys.readouterr().out


def test_cli_manages_mcp_preload_config(tmp_path, capsys) -> None:
    builtins = tmp_path / "builtins"
    _write_mcp_builtin(builtins)
    config = tmp_path / "config.json"
    common = [
        "--config",
        str(config),
        "--registry",
        str(tmp_path / "data" / "plugin-registry.json"),
        "--store-dir",
        str(tmp_path / "data" / "plugin-store"),
        "--plugin-dir",
        str(builtins),
    ]

    assert main([*common, "mcp", "list"]) == 0
    assert "  demo" in capsys.readouterr().out

    assert main([*common, "mcp", "preload", "add", "demo"]) == 0
    assert "preload enabled: demo" in capsys.readouterr().out
    mcp_config = config.parent / "mcp.json"
    assert json.loads(mcp_config.read_text(encoding="utf-8"))["preload"] == ["demo"]

    assert main([*common, "mcp", "list"]) == 0
    assert "* demo" in capsys.readouterr().out

    assert main([*common, "mcp", "preload", "remove", "demo"]) == 0
    assert "preload disabled: demo" in capsys.readouterr().out
    assert json.loads(mcp_config.read_text(encoding="utf-8"))["preload"] == []

    assert main([*common, "mcp", "preload", "add", "missing"]) == 1
    assert "unknown or disabled MCP plugin" in capsys.readouterr().err
