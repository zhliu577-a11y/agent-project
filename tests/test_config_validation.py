# tests/test_config_validation.py —— 插件清单与策略配置 schema 校验（不联网）
from pathlib import Path

import pytest

from plugins.hooks.permission.hook import load_policy
from plugins.loader import discover_plugins, load_mcp_plugins


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _mcp_tree(tmp_path: Path, entry: str, name: str = "x") -> Path:
    return _write(
        tmp_path,
        f"mcp/{name}/plugin.json",
        '{"name": "' + name + '", "type": "mcp", "entry": ' + entry + "}",
    )


def test_manifest_rejects_missing_command(tmp_path) -> None:
    _mcp_tree(tmp_path, "{}")
    with pytest.raises(ValueError, match="command"):
        load_mcp_plugins(tmp_path)


def test_manifest_rejects_bad_type(tmp_path) -> None:
    _write(
        tmp_path,
        "mcp/bad/plugin.json",
        '{"name": "bad", "type": "whatever", "entry": {"command": "python"}}',
    )
    with pytest.raises(ValueError, match="type"):
        discover_plugins(tmp_path)


def test_manifest_rejects_unknown_transport(tmp_path) -> None:
    _mcp_tree(tmp_path, '{"command": "python", "transport": "http"}', name="http")
    with pytest.raises(ValueError, match="transport"):
        load_mcp_plugins(tmp_path)


def test_policy_rejects_invalid_mode(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        "permission.json",
        '{"default": "allow", "rules": [{"tool": "filesystem__delete_file", "mode": "maybe"}]}',
    )
    with pytest.raises(ValueError, match="mode"):
        load_policy(cfg)


def test_policy_rejects_rule_without_tool(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        "permission.json",
        '{"default": "allow", "rules": [{"mode": "deny"}]}',
    )
    with pytest.raises(ValueError, match="tool"):
        load_policy(cfg)
