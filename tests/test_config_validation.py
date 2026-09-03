# tests/test_config_validation.py —— 配置文件 schema 校验测试（同步，不需要联网）
from pathlib import Path

import pytest

from permission import load_policy
from plugin_loader import load_directory


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_plugin_directory_rejects_missing_command(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        "mcp_servers.json",
        '{"plugins": [{"name": "x", "enabled": true, "mcp": {}}]}',
    )
    with pytest.raises(ValueError, match="command"):
        load_directory(cfg)


def test_plugin_directory_rejects_bad_plugins_type(tmp_path) -> None:
    cfg = _write(tmp_path, "mcp_servers.json", '{"plugins": "oops"}')
    with pytest.raises(ValueError, match="plugins"):
        load_directory(cfg)


def test_policy_rejects_invalid_mode(tmp_path) -> None:
    cfg = _write(
        tmp_path,
        "permission.json",
        '{"default": "allow", "rules": [{"tool": "delete_file", "mode": "maybe"}]}',
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
