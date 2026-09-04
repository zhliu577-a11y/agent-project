# tests/test_plugin_loader.py —— 插件目录发现/加载测试（不联网、不启动服务器）
import json
import sys
from pathlib import Path

import pytest

from core.hooks import LifecycleHooks
from plugins.loader import discover_plugins, load_hook_plugins, load_mcp_plugins


def _write_plugin(
    root: Path,
    kind: str,
    name: str,
    manifest: dict,
    files: dict[str, str] | None = None,
) -> Path:
    """在临时目录里搭一个插件目录，返回插件目录路径。"""
    plugin_dir = root / kind / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    for rel, content in (files or {}).items():
        path = plugin_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return plugin_dir


def _mcp_manifest(name: str = "time", enabled: bool = True, **extra) -> dict:
    manifest = {
        "name": name,
        "type": "mcp",
        "description": "测试插件",
        "enabled": enabled,
        "entry": {"command": "python", "args": ["server.py"]},
    }
    manifest.update(extra)
    return manifest


def test_discover_returns_only_enabled_plugins(tmp_path) -> None:
    _write_plugin(tmp_path, "mcp", "time", _mcp_manifest(enabled=True))
    _write_plugin(tmp_path, "mcp", "math", _mcp_manifest(name="math", enabled=False))
    _write_plugin(
        tmp_path,
        "hooks",
        "recorder",
        {
            "name": "recorder",
            "type": "hook",
            "entry": {"module": "hook.py", "factory": "create_hook"},
        },
    )

    manifests = discover_plugins(tmp_path)
    assert [(m.type, m.name) for m in manifests] == [("hook", "recorder"), ("mcp", "time")]


def test_discover_validates_disabled_plugins_too(tmp_path) -> None:
    _write_plugin(tmp_path, "mcp", "future", _mcp_manifest(enabled=False, type="skill"))
    with pytest.raises(ValueError, match="type"):
        discover_plugins(tmp_path)


def test_discover_rejects_duplicate_plugin_name(tmp_path) -> None:
    _write_plugin(tmp_path, "mcp", "time", _mcp_manifest())
    _write_plugin(tmp_path, "mcp", "again", _mcp_manifest())
    with pytest.raises(ValueError, match="重名"):
        discover_plugins(tmp_path)


def test_load_mcp_plugins_resolves_plugin_local_args(tmp_path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "mcp",
        "time",
        _mcp_manifest(),
        files={"server.py": "print('ok')\n"},
    )
    specs = load_mcp_plugins(tmp_path)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.manifest.name == "time"
    assert spec.command == sys.executable  # "python" 替换为当前解释器
    assert Path(spec.args[0]) == plugin_dir / "server.py"  # 相对文件解析为绝对路径


def test_load_mcp_plugins_keeps_literal_args(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "filesystem",
        _mcp_manifest(
            name="filesystem",
            entry={
                "command": "cmd.exe",
                "args": ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "."],
            },
        ),
    )
    spec = load_mcp_plugins(tmp_path)[0]
    assert spec.args == ["/c", "npx", "-y", "@modelcontextprotocol/server-filesystem", "."]


def test_load_mcp_plugins_rejects_unknown_transport(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "http",
        _mcp_manifest(name="http", entry={"command": "x", "transport": "http"}),
    )
    with pytest.raises(ValueError, match="transport"):
        load_mcp_plugins(tmp_path)


_RECORDER_HOOK = """
from pathlib import Path

from core.hooks import LifecycleHooks


class RecorderHooks(LifecycleHooks):
    pass


def create_hook(plugin_dir: Path) -> LifecycleHooks:
    return RecorderHooks()
"""


def test_load_hook_plugins_instantiates_factory(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "hooks",
        "recorder",
        {
            "name": "recorder",
            "type": "hook",
            "entry": {"module": "hook.py", "factory": "create_hook"},
        },
        files={"hook.py": _RECORDER_HOOK},
    )
    hooks = load_hook_plugins(tmp_path)
    assert len(hooks) == 1
    manifest, hook = hooks[0]
    assert manifest.name == "recorder"
    assert isinstance(hook, LifecycleHooks)


def test_load_hook_plugins_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "hooks",
        "bad",
        {
            "name": "bad",
            "type": "hook",
            "entry": {"module": "hook.py", "factory": "create_hook"},
        },
        files={"hook.py": "def create_hook(plugin_dir):\n    return 42\n"},
    )
    with pytest.raises(ValueError, match="LifecycleHooks"):
        load_hook_plugins(tmp_path)


def test_load_hook_plugins_rejects_missing_module(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "hooks",
        "missing",
        {
            "name": "missing",
            "type": "hook",
            "entry": {"module": "nope.py", "factory": "create_hook"},
        },
    )
    with pytest.raises(ValueError, match="入口模块不存在"):
        load_hook_plugins(tmp_path)
