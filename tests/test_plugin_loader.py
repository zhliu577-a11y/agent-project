# tests/test_plugin_loader.py —— 插件目录发现/加载测试（不联网、不启动服务器）
import json
import sys
from pathlib import Path

import pytest

from core.hooks import LifecycleHooks
from plugins.loader import (
    ModelPlugin,
    NamespacedTool,
    assemble_plugins,
    discover_plugins,
    load_hook_plugins,
    load_mcp_plugins,
    load_model_plugins,
    load_skill_plugins,
    load_tool_plugins,
)


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
    _write_plugin(tmp_path, "mcp", "future", _mcp_manifest(enabled=False, type="session"))
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


def _hook_manifest(name: str, priority: int | None = None) -> dict:
    manifest = {
        "name": name,
        "type": "hook",
        "entry": {"module": "hook.py", "factory": "create_hook"},
    }
    if priority is not None:
        manifest["priority"] = priority
    return manifest


def test_load_hook_plugins_sorts_by_priority_then_name(tmp_path) -> None:
    for folder, name, priority in (
        ("zeta", "zeta", 100),
        ("beta", "beta", 0),
        ("alpha", "alpha", 100),
        ("aaa", "aaa", 0),
    ):
        _write_plugin(
            tmp_path,
            "hooks",
            folder,
            _hook_manifest(name, priority),
            files={"hook.py": _RECORDER_HOOK},
        )
    hooks = load_hook_plugins(tmp_path)
    assert [manifest.name for manifest, _ in hooks] == ["aaa", "beta", "alpha", "zeta"]


def test_manifest_rejects_non_int_priority(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "hooks",
        "bad",
        {
            "name": "bad",
            "type": "hook",
            "priority": "first",
            "entry": {"module": "hook.py", "factory": "create_hook"},
        },
    )
    with pytest.raises(ValueError, match="priority"):
        load_hook_plugins(tmp_path)


_TOOL_MODULE = """
from core.tool import Tool


class EchoTool(Tool):
    name = "echo"
    description = "回显文本"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        return "echo:" + kwargs["text"]


class CountTool(Tool):
    name = "count"
    description = "统计长度"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, **kwargs):
        return str(len(kwargs["text"]))


def create_tools(plugin_dir):
    return [EchoTool(), CountTool()]
"""


def _tool_manifest(name: str = "text") -> dict:
    return {
        "name": name,
        "type": "tool",
        "entry": {"module": "tool.py", "factory": "create_tools"},
    }


def test_load_tool_plugins_wraps_tools_with_plugin_namespace(tmp_path) -> None:
    _write_plugin(tmp_path, "tools", "text", _tool_manifest(), files={"tool.py": _TOOL_MODULE})
    plugins = load_tool_plugins(tmp_path)
    assert len(plugins) == 1
    manifest, tools = plugins[0]
    assert manifest.name == "text"
    assert [tool.name for tool in tools] == ["text__echo", "text__count"]
    assert all(isinstance(tool, NamespacedTool) for tool in tools)


def test_load_tool_plugins_accepts_single_tool_return(tmp_path) -> None:
    single = _TOOL_MODULE.replace("return [EchoTool(), CountTool()]", "return EchoTool()")
    _write_plugin(
        tmp_path,
        "tools",
        "single",
        _tool_manifest(name="single"),
        files={"tool.py": single},
    )
    plugins = load_tool_plugins(tmp_path)
    assert [tool.name for tool in plugins[0][1]] == ["single__echo"]


def test_load_tool_plugins_rejects_non_tool_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "tools",
        "bad",
        _tool_manifest(name="bad"),
        files={"tool.py": "def create_tools(plugin_dir):\n    return 'nope'\n"},
    )
    with pytest.raises(ValueError, match="Tool"):
        load_tool_plugins(tmp_path)


def test_load_tool_plugins_rejects_empty_result(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "tools",
        "empty",
        _tool_manifest(name="empty"),
        files={"tool.py": "def create_tools(plugin_dir):\n    return []\n"},
    )
    with pytest.raises(ValueError, match="没有返回任何 Tool"):
        load_tool_plugins(tmp_path)


def test_assemble_plugins_groups_by_kind(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "time",
        {
            "name": "time",
            "type": "mcp",
            "entry": {"command": "python", "args": ["server.py"]},
        },
    )
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
    _write_plugin(tmp_path, "tools", "text", _tool_manifest(), files={"tool.py": _TOOL_MODULE})
    _write_plugin(
        tmp_path,
        "model",
        "deepseek",
        {
            "name": "deepseek",
            "type": "model",
            "entry": {"module": "model.py", "factory": "create_model"},
        },
        files={"model.py": _MODEL_MODULE},
    )
    _write_plugin(
        tmp_path,
        "skills",
        "code-review",
        {
            "name": "code-review",
            "type": "skill",
            "description": "评审规范",
            "entry": {"content": "SKILL.md"},
        },
        files={"SKILL.md": "# 评审清单\n"},
    )

    assembly = assemble_plugins(tmp_path)
    assert [manifest.name for manifest, _ in assembly.hooks] == ["recorder"]
    assert [spec.manifest.name for spec in assembly.mcp] == ["time"]
    assert [manifest.name for manifest, _ in assembly.tools] == ["text"]
    assert [plugin.manifest.name for plugin in assembly.models] == ["deepseek"]
    assert [plugin.manifest.name for plugin in assembly.skills] == ["code-review"]


_MODEL_MODULE = """
from core.model import ModelAdapter
from core.types import Message, ModelResponse


class FakeModelAdapter(ModelAdapter):
    async def complete(self, messages, tool_schemas, on_token=None):
        return ModelResponse(content="fake", tool_calls=[])


def create_model(plugin_dir):
    return FakeModelAdapter()
"""


def _model_manifest(name: str = "deepseek") -> dict:
    return {
        "name": name,
        "type": "model",
        "entry": {"module": "model.py", "factory": "create_model"},
    }


def test_load_model_plugins_is_lazy_and_create_returns_adapter(tmp_path) -> None:
    _write_plugin(
        tmp_path, "model", "deepseek", _model_manifest(), files={"model.py": _MODEL_MODULE}
    )
    plugins = load_model_plugins(tmp_path)
    assert len(plugins) == 1
    plugin = plugins[0]
    assert isinstance(plugin, ModelPlugin)
    assert plugin.manifest.name == "deepseek"
    # 装配阶段不实例化（模型工厂有环境变量副作用），create() 时才创建
    model = plugin.create()
    assert type(model).__name__ == "FakeModelAdapter"


def test_load_model_plugin_rejects_missing_entry(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "model",
        "bad",
        {"name": "bad", "type": "model", "entry": {}},
    )
    with pytest.raises(ValueError, match="model 插件必须在 entry"):
        load_model_plugins(tmp_path)


def test_model_plugin_create_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "model",
        "bad",
        _model_manifest(name="bad"),
        files={"model.py": "def create_model(plugin_dir):\n    return 42\n"},
    )
    plugin = load_model_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="ModelAdapter"):
        plugin.create()


def _skill_manifest(name: str = "code-review", **extra) -> dict:
    manifest = {
        "name": name,
        "type": "skill",
        "description": "评审规范",
        "entry": {"content": "SKILL.md"},
    }
    manifest.update(extra)
    return manifest


def test_load_skill_plugins_points_to_content_file(tmp_path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "skills",
        "code-review",
        _skill_manifest(),
        files={"SKILL.md": "# 评审\n正文"},
    )
    plugins = load_skill_plugins(tmp_path)
    assert len(plugins) == 1
    plugin = plugins[0]
    assert plugin.manifest.name == "code-review"
    assert plugin.content_path == plugin_dir / "SKILL.md"
    assert plugin.preload is False


def test_load_skill_plugins_rejects_missing_content_file(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "broken",
        _skill_manifest(name="broken", entry={"content": "nope.md"}),
    )
    with pytest.raises(ValueError, match="正文文件不存在"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_rejects_bad_preload_type(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "bad-preload",
        _skill_manifest(name="bad-preload", entry={"content": "SKILL.md", "preload": "yes"}),
        files={"SKILL.md": "# x\n"},
    )
    with pytest.raises(ValueError, match="preload"):
        load_skill_plugins(tmp_path)
