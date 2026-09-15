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
    assert "quality\t1.0.0\tenabled" in capsys.readouterr().out
