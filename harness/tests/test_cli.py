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
                        "contract": "skill.v1",
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


def _write_skill_builtin(root: Path, *, description: str = "code review") -> None:
    package = root / "skills" / "review"
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(
        json.dumps(
            {
                "name": "review",
                "type": "skill",
                "contract": "skill.v1",
                "description": description,
                "entry": {"content": "SKILL.md"},
            }
        ),
        encoding="utf-8",
    )
    (package / "SKILL.md").write_text("# review\n", encoding="utf-8")


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


def test_cli_lists_shows_and_searches_skills(tmp_path, capsys) -> None:
    builtins = tmp_path / "builtins"
    _write_skill_builtin(builtins)
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

    assert main([*common, "skill", "list"]) == 0
    assert "review" in capsys.readouterr().out

    assert main([*common, "skill", "show", "review"]) == 0
    assert "# review" in capsys.readouterr().out

    assert main([*common, "skill", "search", "code"]) == 0
    assert "review" in capsys.readouterr().out


def test_cli_skill_ask_requires_manual_approval(tmp_path, capsys) -> None:
    builtins = tmp_path / "builtins"
    _write_skill_builtin(builtins)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text("{}", encoding="utf-8")
    (config_dir / "skill.json").write_text(
        json.dumps({"permissions": {"review": "ask"}}),
        encoding="utf-8",
    )
    common = [
        "--config",
        str(config_dir),
        "--registry",
        str(tmp_path / "data" / "plugin-registry.json"),
        "--store-dir",
        str(tmp_path / "data" / "plugin-store"),
        "--plugin-dir",
        str(builtins),
    ]

    assert main([*common, "skill", "show", "review"]) == 1
    assert "requires --approve" in capsys.readouterr().err

    assert main([*common, "skill", "show", "review", "--approve"]) == 0
    assert "# review" in capsys.readouterr().out
