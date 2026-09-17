import json
import zipfile
from pathlib import Path

import pytest

from plugins.manager import PluginManager


def _write_package(root: Path, name: str = "quality", version: str = "1.0.0") -> Path:
    package_dir = root / name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "name": name,
                "version": version,
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
    (package_dir / "SKILL.md").write_text("# lint\n", encoding="utf-8")
    return package_dir


def _manager(tmp_path: Path) -> PluginManager:
    builtin = tmp_path / "builtins"
    builtin.mkdir(exist_ok=True)
    return PluginManager(
        registry_path=tmp_path / "data" / "plugin-registry.json",
        store_dir=tmp_path / "data" / "plugin-store",
        builtin_dir=builtin,
    )


def test_validate_directory_does_not_install(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")

    inspection = manager.validate(package)

    assert inspection.name == "quality"
    assert manager.list_installed() == ()
    assert not (manager.store_dir / "installed").exists()


def test_install_is_disabled_and_writes_registry(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")

    record = manager.install(package)

    assert record.enabled is False
    assert record.installed is True
    assert record.runtime_status == "disabled"
    assert record.digest.startswith("sha256:")
    assert (manager.installed_dir / "quality" / "1.0.0" / "plugin.json").is_file()
    assert manager.list_installed() == (record,)

    saved = json.loads(manager.registry_path.read_text(encoding="utf-8"))
    assert saved["apiVersion"] == 1
    assert saved["plugins"]["quality"]["enabled"] is False


def test_enable_disable_and_runtime_roots_follow_registry(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")
    manager.install(package)

    assert manager.runtime_roots() == [manager.builtin_dir]

    enabled = manager.enable("quality")
    assert enabled.enabled is True
    assert enabled.runtime_status == "idle"
    assert manager.runtime_roots()[-1] == (manager.installed_dir / "quality" / "1.0.0").resolve()

    disabled = manager.disable("quality")
    assert disabled.enabled is False
    assert disabled.runtime_status == "disabled"
    assert manager.runtime_roots() == [manager.builtin_dir]


def test_install_zip_package(tmp_path) -> None:
    manager = _manager(tmp_path)
    source = _write_package(tmp_path / "source")
    archive = tmp_path / "quality.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.write(source / "plugin.json", "quality/plugin.json")
        writer.write(source / "SKILL.md", "quality/SKILL.md")

    record = manager.install(archive)

    assert record.name == "quality"
    assert manager.validate(archive).version == "1.0.0"


def test_install_rejects_duplicate_package(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")
    manager.install(package)

    with pytest.raises(ValueError, match="already installed"):
        manager.install(package)


def test_zip_rejects_path_traversal(tmp_path) -> None:
    manager = _manager(tmp_path)
    archive = tmp_path / "escape.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("../plugin.json", "{}")

    with pytest.raises(ValueError, match="escapes package root"):
        manager.validate(archive)


def test_remove_moves_package_to_trash(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")
    record = manager.install(package)
    installed_path = manager._resolve_record_path(record)

    manager.remove("quality")

    assert manager.list_installed() == ()
    assert not installed_path.exists()
    assert len(list((manager.store_dir / "trash").iterdir())) == 1


def test_registry_path_cannot_escape_installed_store(tmp_path) -> None:
    registry_path = tmp_path / "data" / "plugin-registry.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(
        json.dumps(
            {
                "apiVersion": 1,
                "plugins": {
                    "escape": {
                        "installed": True,
                        "enabled": True,
                        "version": "1.0.0",
                        "source": "test",
                        "path": "../../outside",
                        "digest": "",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(
        registry_path=registry_path,
        store_dir=tmp_path / "data" / "plugin-store",
        builtin_dir=tmp_path / "builtins",
    )

    with pytest.raises(ValueError, match="outside installed store"):
        manager.runtime_roots()


def test_record_runtime_status_requires_known_status(tmp_path) -> None:
    manager = _manager(tmp_path)
    package = _write_package(tmp_path / "source")
    manager.install(package)

    with pytest.raises(ValueError, match="unsupported runtime status"):
        manager.record_runtime_status("quality", "working")
