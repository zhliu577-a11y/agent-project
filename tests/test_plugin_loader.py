# tests/test_plugin_loader.py —— 插件目录发现/加载测试（不联网、不启动服务器）
import json
import sys
from pathlib import Path

import pytest

from config import AppConfig
from core.hooks import LifecycleHooks
from plugins import loader as plugin_loader
from plugins.loader import (
    KindHandler,
    ModelPlugin,
    NamespacedTool,
    assemble_plugins,
    discover_contributions,
    discover_plugins,
    inspect_package,
    load_context_plugins,
    load_embedding_plugins,
    load_hook_plugins,
    load_mcp_plugins,
    load_memory_plugins,
    load_model_plugins,
    load_model_router_plugins,
    load_session_plugins,
    load_skill_plugins,
    load_tool_plugins,
    register_kind,
)
from plugins.services import RuntimeServices


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
    _write_plugin(tmp_path, "mcp", "future", _mcp_manifest(enabled=False, type="bundle"))
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


def test_load_hook_plugins_accepts_user_prompt_control_event(tmp_path) -> None:
    manifest = _hook_manifest("prompt-policy")
    manifest["hook"] = {"events": ["user_prompt_submit"]}
    _write_plugin(
        tmp_path,
        "hooks",
        "prompt-policy",
        manifest,
        files={"hook.py": _RECORDER_HOOK},
    )

    loaded = load_hook_plugins(tmp_path)

    assert loaded[0][0].hook is not None
    assert loaded[0][0].hook.events == ("user_prompt_submit",)


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


def test_manifest_parses_declared_errors(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "time",
        _mcp_manifest(
            errors=[
                {"code": "rate_limited", "category": "retryable", "hint": "稍后重试"},
                {"code": "bad_args", "category": "tool"},
            ]
        ),
    )
    manifest = discover_plugins(tmp_path)[0]
    assert [declared.code for declared in manifest.errors] == ["rate_limited", "bad_args"]
    assert manifest.errors[0].retryable is True
    assert manifest.errors[1].retryable is False
    assert manifest.errors[0].hint == "稍后重试"


def test_manifest_rejects_duplicate_error_code(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "dup",
        _mcp_manifest(
            name="dup",
            errors=[
                {"code": "boom", "category": "tool"},
                {"code": "boom", "category": "tool"},
            ],
        ),
    )
    with pytest.raises(ValueError, match="错误码重复"):
        discover_plugins(tmp_path)


def test_manifest_rejects_unknown_error_category(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "badcat",
        _mcp_manifest(name="badcat", errors=[{"code": "boom", "category": "whatever"}]),
    )
    with pytest.raises(ValueError, match="category"):
        discover_plugins(tmp_path)


def test_manifest_rejects_retryable_mismatch(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "mcp",
        "mismatch",
        _mcp_manifest(
            name="mismatch",
            errors=[{"code": "boom", "category": "tool", "retryable": True}],
        ),
    )
    with pytest.raises(ValueError, match="retryable"):
        discover_plugins(tmp_path)


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
        "context",
        "tail-window",
        _context_manifest(),
        files={"policy.py": _CONTEXT_MODULE},
    )
    _write_plugin(
        tmp_path,
        "skills",
        "code-review",
        {
            "name": "code-review",
            "type": "skill",
            "contract": "skill.v1",
            "description": "评审规范",
            "entry": {"content": "SKILL.md"},
        },
        files={"SKILL.md": "# 评审清单\n"},
    )
    _write_plugin(
        tmp_path,
        "session",
        "jsonl",
        {
            "name": "jsonl",
            "type": "session",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        files={"store.py": _SESSION_MODULE},
    )
    _write_plugin(
        tmp_path,
        "memory",
        "jsonl",
        {
            "name": "jsonl",
            "type": "memory",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        files={"store.py": _MEMORY_MODULE},
    )
    _write_plugin(
        tmp_path,
        "embedding",
        "debug",
        {
            "name": "debug",
            "type": "embedding",
            "entry": {"module": "provider.py", "factory": "create_provider"},
        },
        files={"provider.py": _EMBEDDING_MODULE},
    )

    assembly = assemble_plugins(tmp_path)
    assert [manifest.name for manifest, _ in assembly.hooks] == ["recorder"]
    assert [spec.manifest.name for spec in assembly.mcp] == ["time"]
    assert [manifest.name for manifest, _ in assembly.tools] == ["text"]
    assert [plugin.manifest.name for plugin in assembly.models] == ["deepseek"]
    assert [plugin.manifest.name for plugin in assembly.contexts] == ["tail-window"]
    assert [plugin.manifest.name for plugin in assembly.skills] == ["code-review"]
    assert [plugin.manifest.name for plugin in assembly.sessions] == ["jsonl"]
    assert [plugin.manifest.name for plugin in assembly.memories] == ["jsonl"]
    assert [plugin.manifest.name for plugin in assembly.embeddings] == ["debug"]


_MODEL_MODULE = """
from core.model import ModelAdapter
from core.types import Message, ModelResponse


class FakeModelAdapter(ModelAdapter):
    async def complete(self, messages, tool_schemas, on_token=None):
        return ModelResponse(content="fake", tool_calls=[])


def create_model(plugin_dir):
    return FakeModelAdapter()
"""


def _model_manifest(name: str = "deepseek", **extra) -> dict:
    manifest = {
        "name": name,
        "type": "model",
        "entry": {"module": "model.py", "factory": "create_model"},
    }
    manifest.update(extra)
    return manifest


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


def test_model_manifest_parses_capability_metadata(tmp_path) -> None:
    manifest = _model_manifest(
        name="routed",
        model={
            "roles": ["coding", "reasoning"],
            "capabilities": ["text", "tools"],
            "contextWindow": 64000,
            "supportsTools": True,
            "supportsStreaming": False,
            "costTier": "low",
            "latencyTier": "high",
        },
    )
    _write_plugin(
        tmp_path,
        "model",
        "routed",
        manifest,
        files={"model.py": _MODEL_MODULE},
    )

    parsed = load_model_plugins(tmp_path)[0].manifest.model

    assert parsed is not None
    assert parsed.roles == ("coding", "reasoning")
    assert parsed.supports("tools") is True
    assert parsed.supports("streaming") is False
    assert parsed.context_window == 64000


_MODEL_ROUTER_MODULE = """
from core.model import ModelRouteDecision, ModelRouter


class StaticRouter(ModelRouter):
    def route(self, request):
        return ModelRouteDecision(candidates=(request.default_model,))


def create_router(plugin_dir):
    return StaticRouter()
"""


def test_load_model_router_plugins_is_lazy_and_returns_router(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "model_routers",
        "static",
        {
            "name": "static",
            "type": "model-router",
            "entry": {"module": "router.py", "factory": "create_router"},
        },
        files={"router.py": _MODEL_ROUTER_MODULE},
    )

    plugins = load_model_router_plugins(tmp_path)

    assert len(plugins) == 1
    assert type(plugins[0].create()).__name__ == "StaticRouter"


def test_model_router_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "model_routers",
        "bad",
        {
            "name": "bad",
            "type": "model-router",
            "entry": {"module": "router.py", "factory": "create_router"},
        },
        files={"router.py": "def create_router(plugin_dir):\n    return 42\n"},
    )

    with pytest.raises(ValueError, match="ModelRouter"):
        load_model_router_plugins(tmp_path)[0].create()


_CONTEXT_MODULE = """
from core.context import ContextResult, TailWindowPolicy


class FakeContextPolicy(TailWindowPolicy):
    async def prepare(self, request):
        result = await super().prepare(request)
        return ContextResult(
            messages=result.messages,
            dropped_count=result.dropped_count,
            metadata={"strategy": "fake"},
        )


def create_policy(plugin_dir):
    return FakeContextPolicy()
"""


def _context_manifest(name: str = "tail-window") -> dict:
    return {
        "name": name,
        "type": "context",
        "entry": {"module": "policy.py", "factory": "create_policy"},
    }


def test_load_context_plugins_is_lazy_and_create_returns_policy(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "context",
        "tail-window",
        _context_manifest(),
        files={"policy.py": _CONTEXT_MODULE},
    )
    plugins = load_context_plugins(tmp_path)
    assert len(plugins) == 1
    policy = plugins[0].create()
    assert type(policy).__name__ == "FakeContextPolicy"


def test_context_plugin_create_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "context",
        "bad",
        _context_manifest(name="bad"),
        files={"policy.py": "def create_policy(plugin_dir):\n    return 42\n"},
    )
    plugin = load_context_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="ContextPolicy"):
        plugin.create()


def _skill_manifest(name: str = "code-review", **extra) -> dict:
    manifest = {
        "name": name,
        "type": "skill",
        "contract": "skill.v1",
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


def test_skill_v2_imports_frontmatter_with_plugin_json_precedence(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "review",
        _skill_manifest(
            name="review",
            protocolVersion=2,
            contract="skill.v2",
            description="内部描述",
            entry={
                "content": "SKILL.md",
                "importFrontmatter": True,
            },
        ),
        files={
            "SKILL.md": (
                "---\n"
                "name: review\n"
                "description: 外部描述\n"
                "when_to_use: 检查代码质量\n"
                "tags: [python, security]\n"
                "compatibility: harness >= 1\n"
                "license: MIT\n"
                "metadata:\n"
                "  owner: platform\n"
                "---\n"
                "# review\n"
            )
        },
    )

    plugin = load_skill_plugins(tmp_path)[0]

    assert plugin.description == "内部描述"
    assert plugin.when_to_use == "检查代码质量"
    assert plugin.tags == ("python", "security")
    assert plugin.compatibility == "harness >= 1"
    assert plugin.license == "MIT"
    assert plugin.metadata == (("owner", "platform"),)


def test_skill_v1_rejects_v2_metadata(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "review",
        _skill_manifest(
            name="review",
            entry={
                "content": "SKILL.md",
                "when_to_use": "检查代码",
            },
        ),
        files={"SKILL.md": "# review\n"},
    )

    with pytest.raises(ValueError, match="protocolVersion 2"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_requires_explicit_contract(tmp_path) -> None:
    manifest = _skill_manifest()
    manifest.pop("contract")
    _write_plugin(
        tmp_path,
        "skills",
        "missing-contract",
        manifest,
        files={"SKILL.md": "# review\n"},
    )

    with pytest.raises(ValueError, match="missing required 'contract'"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_rejects_multiline_description(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "multiline",
        _skill_manifest(name="multiline", description="第一行\n第二行"),
        files={"SKILL.md": "# review\n"},
    )

    with pytest.raises(ValueError, match="单行可打印文本"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_enforces_description_budget(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "long-description",
        _skill_manifest(name="long-description", description="description"),
        files={"SKILL.md": "# review\n"},
    )

    with pytest.raises(ValueError, match="description超过大小预算"):
        load_skill_plugins(
            tmp_path,
            AppConfig(skill_max_description_bytes=4),
        )


def test_load_skill_plugins_rejects_multiline_resource_description(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "bad-resource-description",
        _skill_manifest(
            name="bad-resource-description",
            entry={
                "content": "SKILL.md",
                "resources": [
                    {"path": "references/api.md", "description": "第一行\n第二行"},
                ],
            },
        ),
        files={"SKILL.md": "# review\n", "references/api.md": "# API\n"},
    )

    with pytest.raises(ValueError, match="资源 description必须是单行可打印文本"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_enforces_resource_description_budget(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "long-resource-description",
        _skill_manifest(
            name="long-resource-description",
            entry={
                "content": "SKILL.md",
                "resources": [
                    {"path": "references/api.md", "description": "description"},
                ],
            },
        ),
        files={"SKILL.md": "# review\n", "references/api.md": "# API\n"},
    )

    with pytest.raises(ValueError, match="资源 description超过大小预算"):
        load_skill_plugins(
            tmp_path,
            AppConfig(skill_max_resource_description_bytes=4),
        )


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


def test_load_skill_plugins_rejects_content_path_outside_plugin_dir(tmp_path) -> None:
    secret = tmp_path / "skills" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret", encoding="utf-8")
    _write_plugin(
        tmp_path,
        "skills",
        "escape",
        _skill_manifest(name="escape", entry={"content": "../secret.md"}),
    )

    with pytest.raises(ValueError, match="越出插件目录"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_validates_resources_inside_plugin_dir(tmp_path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "skills",
        "with-resource",
        _skill_manifest(
            name="with-resource",
            entry={
                "content": "SKILL.md",
                "resources": [
                    {"path": "references/api.md", "description": "API 参考"},
                ],
            },
        ),
        files={"SKILL.md": "# skill\n", "references/api.md": "# API\n"},
    )

    plugin = load_skill_plugins(tmp_path)[0]

    assert len(plugin.resources) == 1
    assert plugin.resources[0].path == "references/api.md"
    assert plugin.resources[0].description == "API 参考"
    assert plugin.resources[0].resolved_path == plugin_dir / "references" / "api.md"


def test_load_skill_plugins_rejects_resource_path_outside_plugin_dir(tmp_path) -> None:
    secret = tmp_path / "skills" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("secret", encoding="utf-8")
    _write_plugin(
        tmp_path,
        "skills",
        "escape",
        _skill_manifest(
            name="escape",
            entry={
                "content": "SKILL.md",
                "resources": [{"path": "../secret.md"}],
            },
        ),
        files={"SKILL.md": "# skill\n"},
    )

    with pytest.raises(ValueError, match="越出插件目录"):
        load_skill_plugins(tmp_path)


def test_load_skill_plugins_enforces_content_budget(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "skills",
        "large",
        _skill_manifest(name="large"),
        files={"SKILL.md": "12345"},
    )

    with pytest.raises(ValueError, match="正文超过大小预算"):
        load_skill_plugins(tmp_path, AppConfig(skill_max_content_bytes=4))


def test_load_skill_plugins_enforces_resource_budgets(tmp_path) -> None:
    manifest = _skill_manifest(
        name="large-resource",
        entry={
            "content": "SKILL.md",
            "resources": [{"path": "references/api.md"}],
        },
    )
    _write_plugin(
        tmp_path,
        "skills",
        "large-resource",
        manifest,
        files={"SKILL.md": "# skill\n", "references/api.md": "12345"},
    )

    with pytest.raises(ValueError, match="单文件大小预算"):
        load_skill_plugins(tmp_path, AppConfig(skill_max_resource_bytes=4))


def test_load_skill_plugins_enforces_resource_count_budget(tmp_path) -> None:
    manifest = _skill_manifest(
        name="many-resources",
        entry={
            "content": "SKILL.md",
            "resources": [
                {"path": "references/one.md"},
                {"path": "references/two.md"},
            ],
        },
    )
    _write_plugin(
        tmp_path,
        "skills",
        "many-resources",
        manifest,
        files={
            "SKILL.md": "# skill\n",
            "references/one.md": "1",
            "references/two.md": "2",
        },
    )

    with pytest.raises(ValueError, match="资源数量超过预算"):
        load_skill_plugins(tmp_path, AppConfig(skill_max_resources=1))


def test_load_skill_plugins_enforces_resource_total_budget(tmp_path) -> None:
    manifest = _skill_manifest(
        name="total-resource",
        entry={
            "content": "SKILL.md",
            "resources": [
                {"path": "references/one.md"},
                {"path": "references/two.md"},
            ],
        },
    )
    _write_plugin(
        tmp_path,
        "skills",
        "total-resource",
        manifest,
        files={
            "SKILL.md": "# skill\n",
            "references/one.md": "123",
            "references/two.md": "456",
        },
    )

    with pytest.raises(ValueError, match="资源总大小超过预算"):
        load_skill_plugins(
            tmp_path,
            AppConfig(
                skill_max_resource_bytes=3,
                skill_max_resource_total_bytes=5,
            ),
        )


_SESSION_MODULE = """
from core.session import SessionStore


class FakeStore(SessionStore):
    async def load(self, session_id):
        return []

    async def save(self, session_id, messages):
        pass

    async def load_checkpoint(self, session_id):
        return None

    async def save_checkpoint(self, session_id, snapshot):
        pass

    async def delete_checkpoint(self, session_id):
        pass


def create_store(plugin_dir):
    return FakeStore()
"""


def _session_manifest(name: str = "memory") -> dict:
    return {
        "name": name,
        "type": "session",
        "entry": {"module": "store.py", "factory": "create_store"},
    }


def test_load_session_plugins_is_lazy_and_create_returns_store(tmp_path) -> None:
    _write_plugin(
        tmp_path, "session", "memory", _session_manifest(), files={"store.py": _SESSION_MODULE}
    )
    plugins = load_session_plugins(tmp_path)
    assert len(plugins) == 1
    store = plugins[0].create()
    assert type(store).__name__ == "FakeStore"


def test_session_plugin_create_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "session",
        "bad",
        _session_manifest(name="bad"),
        files={"store.py": "def create_store(plugin_dir):\n    return 42\n"},
    )
    plugin = load_session_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="SessionStore"):
        plugin.create()


_MEMORY_MODULE = """
from core.memory import MemoryStore


class FakeMemory(MemoryStore):
    async def list_notes(self):
        return []

    async def add_note(self, content, tags):
        pass

    async def delete_note(self, note_id):
        return False

    async def update_note(self, note_id, content=None, tags=None):
        return None

    async def search_notes(self, query):
        return []


def create_store(plugin_dir):
    return FakeMemory()
"""


def test_load_memory_plugins_is_lazy_and_create_returns_store(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "memory",
        "jsonl",
        {
            "name": "jsonl",
            "type": "memory",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        files={"store.py": _MEMORY_MODULE},
    )
    plugins = load_memory_plugins(tmp_path)
    assert len(plugins) == 1
    store = plugins[0].create()
    assert type(store).__name__ == "FakeMemory"


def test_memory_plugin_create_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "memory",
        "bad",
        {
            "name": "bad",
            "type": "memory",
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        files={"store.py": "def create_store(plugin_dir):\n    return 42\n"},
    )
    plugin = load_memory_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="MemoryStore"):
        plugin.create()


_EMBEDDING_MODULE = """
from core.embedding import EmbeddingProvider


class FakeEmbedding(EmbeddingProvider):
    async def embed(self, texts):
        return [[0.1, 0.2]] * len(texts)


def create_provider(plugin_dir):
    return FakeEmbedding()
"""


def test_load_embedding_plugins_is_lazy_and_create_returns_provider(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "embedding",
        "debug",
        {
            "name": "debug",
            "type": "embedding",
            "entry": {"module": "provider.py", "factory": "create_provider"},
        },
        files={"provider.py": _EMBEDDING_MODULE},
    )
    plugins = load_embedding_plugins(tmp_path)
    assert len(plugins) == 1
    provider = plugins[0].create()
    assert type(provider).__name__ == "FakeEmbedding"


def test_embedding_plugin_create_rejects_bad_factory_return(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "embedding",
        "bad",
        {
            "name": "bad",
            "type": "embedding",
            "entry": {"module": "provider.py", "factory": "create_provider"},
        },
        files={"provider.py": "def create_provider(plugin_dir):\n    return 42\n"},
    )
    plugin = load_embedding_plugins(tmp_path)[0]
    with pytest.raises(ValueError, match="EmbeddingProvider"):
        plugin.create()


_DEPENDENT_MEMORY_MODULE = """
from core.memory import MemoryStore


class DependentMemory(MemoryStore):
    def __init__(self, embedding):
        self.embedding = embedding

    async def list_notes(self):
        return []

    async def add_note(self, content, tags):
        pass

    async def delete_note(self, note_id):
        return False

    async def update_note(self, note_id, content=None, tags=None):
        return None

    async def search_notes(self, query):
        return []


def create_store(plugin_dir, embedding):
    return DependentMemory(embedding)
"""


def test_manifest_requirements_inject_runtime_services(tmp_path) -> None:
    _write_plugin(
        tmp_path,
        "memory",
        "dependent",
        {
            "name": "dependent",
            "type": "memory",
            "requires": [
                {
                    "kind": "embedding",
                    "name": "debug",
                    "contract": "embedding.v1",
                    "inject": "embedding",
                }
            ],
            "entry": {"module": "store.py", "factory": "create_store"},
        },
        files={"store.py": _DEPENDENT_MEMORY_MODULE},
    )
    services = RuntimeServices()
    services.select("embedding", "debug")
    embedding = object()
    services.register("embedding", "debug", embedding)

    plugin = load_memory_plugins(tmp_path, services=services)[0]

    assert plugin.manifest.requires[0].injection_name == "embedding"
    assert plugin.create().embedding is embedding


def _write_package(root: Path, name: str, manifest: dict, files: dict[str, str]) -> Path:
    package_dir = root / "packages" / name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    for rel, content in files.items():
        path = package_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package_dir


def test_package_manifest_expands_to_contributions(tmp_path) -> None:
    _write_package(
        tmp_path,
        "quality",
        {
            "apiVersion": "1",
            "name": "quality",
            "version": "1.2.0",
            "description": "quality package",
            "contributes": [
                {
                    "id": "lint",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "description": "lint skill",
                    "entry": {"content": "skills/lint/SKILL.md"},
                },
                {
                    "id": "text-tools",
                    "kind": "tool",
                    "entry": {"module": "tool.py", "factory": "create_tools"},
                },
            ],
        },
        {
            "skills/lint/SKILL.md": "# lint\n",
            "tool.py": _TOOL_MODULE,
        },
    )

    contributions = discover_contributions(tmp_path)
    assert [(c.package_name, c.contribution_id, c.kind) for c in contributions] == [
        ("quality", "lint", "skill"),
        ("quality", "text-tools", "tool"),
    ]
    assert [m.name for m in discover_plugins(tmp_path)] == ["quality--lint", "quality--text-tools"]

    assembly = assemble_plugins(tmp_path)
    assert [c.contribution_id for c in assembly.contributions] == ["lint", "text-tools"]
    assert [plugin.manifest.name for plugin in assembly.skills] == ["quality--lint"]
    assert [manifest.name for manifest, _ in assembly.tools] == ["quality--text-tools"]
    assert [tool.name for tool in assembly.tools[0][1]] == [
        "quality--text-tools__echo",
        "quality--text-tools__count",
    ]


def test_package_can_contribute_multiple_skills(tmp_path) -> None:
    _write_package(
        tmp_path,
        "quality",
        {
            "apiVersion": "1",
            "name": "quality",
            "version": "1.0.0",
            "contributes": [
                {
                    "id": "lint",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "skills/lint/SKILL.md"},
                },
                {
                    "id": "review",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "skills/review/SKILL.md"},
                },
            ],
        },
        {
            "skills/lint/SKILL.md": "# lint\n",
            "skills/review/SKILL.md": "# review\n",
        },
    )

    assembly = assemble_plugins(tmp_path)

    assert [plugin.manifest.name for plugin in assembly.skills] == [
        "quality--lint",
        "quality--review",
    ]


def test_discover_accepts_package_root_with_manifest(tmp_path) -> None:
    package_dir = _write_package(
        tmp_path,
        "quality",
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
        },
        {"SKILL.md": "# lint\n"},
    )

    manifests = discover_plugins(package_dir)

    assert [manifest.name for manifest in manifests] == ["quality--lint"]


def test_disabled_package_is_validated_but_not_returned(tmp_path) -> None:
    _write_package(
        tmp_path,
        "disabled",
        {
            "apiVersion": "1",
            "name": "disabled",
            "enabled": False,
            "contributes": [
                {
                    "id": "lint",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "SKILL.md"},
                }
            ],
        },
        {"SKILL.md": "# lint\n"},
    )
    assert discover_contributions(tmp_path) == []


def test_inspect_package_validates_entries_without_importing_code(tmp_path) -> None:
    package_dir = _write_package(
        tmp_path,
        "quality",
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
        },
        {"SKILL.md": "# lint\n"},
    )

    inspection = inspect_package(package_dir)

    assert inspection.name == "quality"
    assert inspection.version == "1.0.0"
    assert [manifest.name for manifest in inspection.manifests] == ["quality--lint"]
    assert [item.contribution_id for item in inspection.contributions] == ["lint"]


def test_inspect_package_rejects_entry_outside_package(tmp_path) -> None:
    package_dir = _write_package(
        tmp_path,
        "escape",
        {
            "apiVersion": "1",
            "name": "escape",
            "contributes": [
                {
                    "id": "lint",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "../outside.md"},
                }
            ],
        },
        {},
    )

    with pytest.raises(ValueError, match="越出插件目录"):
        inspect_package(package_dir)


def test_discover_accepts_multiple_plugin_roots(tmp_path) -> None:
    first = tmp_path / "builtins"
    second = tmp_path / "installed"
    _write_plugin(first, "mcp", "time", _mcp_manifest())
    _write_plugin(second, "mcp", "math", _mcp_manifest(name="math"))

    manifests = discover_plugins([first, second])

    assert [manifest.name for manifest in manifests] == ["math", "time"]


def test_package_rejects_unknown_contribution_kind_even_when_disabled(tmp_path) -> None:
    _write_package(
        tmp_path,
        "bad-kind",
        {
            "apiVersion": "1",
            "name": "bad-kind",
            "enabled": False,
            "contributes": [
                {
                    "id": "future",
                    "kind": "workflow",
                    "entry": {},
                }
            ],
        },
        {},
    )
    with pytest.raises(ValueError, match="kind"):
        discover_contributions(tmp_path)


def test_package_rejects_duplicate_contribution_id(tmp_path) -> None:
    _write_package(
        tmp_path,
        "duplicate",
        {
            "apiVersion": "1",
            "name": "duplicate",
            "contributes": [
                {
                    "id": "same",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "SKILL.md"},
                },
                {
                    "id": "same",
                    "kind": "skill",
                    "contract": "skill.v1",
                    "entry": {"content": "SKILL.md"},
                },
            ],
        },
        {"SKILL.md": "# lint\n"},
    )
    with pytest.raises(ValueError, match="contribution id"):
        discover_contributions(tmp_path)


def test_package_rejects_duplicate_package_name(tmp_path) -> None:
    manifest = {
        "apiVersion": "1",
        "name": "quality",
        "contributes": [
            {
                "id": "lint",
                "kind": "skill",
                "contract": "skill.v1",
                "entry": {"content": "SKILL.md"},
            }
        ],
    }
    _write_package(tmp_path, "quality-a", manifest, {"SKILL.md": "# lint\n"})
    _write_package(tmp_path, "quality-b", manifest, {"SKILL.md": "# lint\n"})

    with pytest.raises(ValueError, match="功能包重名"):
        discover_contributions(tmp_path)


def test_package_can_contribute_listener_with_inherited_events(tmp_path) -> None:
    _write_package(
        tmp_path,
        "observability",
        {
            "apiVersion": "1",
            "name": "observability",
            "events": ["tool.after"],
            "contributes": [
                {
                    "id": "timeline",
                    "kind": "listener",
                    "entry": {"module": "listener.py", "factory": "create_listener"},
                }
            ],
        },
        {
            "listener.py": (
                "from core.events import Subscription\n\n"
                "def create_listener(plugin_dir):\n"
                "    return [Subscription(event='tool.after', handler=lambda event: None)]\n"
            )
        },
    )

    assembly = assemble_plugins(tmp_path)

    assert [plugin.manifest.name for plugin in assembly.listeners] == ["observability--timeline"]
    assert assembly.listeners[0].manifest.events == ("tool.after",)
    assert assembly.listeners[0].create()[0].event == "tool.after"
    assert assembly.contributions[0].package_name == "observability"


def test_assemble_plugins_uses_kinds_registered_after_import(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(plugin_loader, "_KIND_REGISTRY", dict(plugin_loader._KIND_REGISTRY))
    monkeypatch.setattr(plugin_loader, "SUPPORTED_KINDS", plugin_loader.SUPPORTED_KINDS)
    seen: list[tuple[str, str]] = []
    register_kind(
        KindHandler(
            "test-kind",
            lambda manifest: manifest.name,
            lambda _assembly, manifest, payload: seen.append((manifest.name, payload)),
        ),
        replace=True,
    )
    _write_plugin(
        tmp_path,
        "custom",
        "demo",
        {"name": "demo", "type": "test-kind", "entry": {}},
    )

    assembly = assemble_plugins(tmp_path)

    assert seen == [("demo", "demo")]
    assert [(c.kind, c.manifest.name) for c in assembly.contributions] == [("test-kind", "demo")]


def test_repository_context_strategies_are_loadable() -> None:
    context_root = Path(__file__).resolve().parents[1] / "plugins" / "context"
    plugins = load_context_plugins(context_root)

    assert [plugin.manifest.name for plugin in plugins] == [
        "memory-tail-window",
        "summary-window",
        "tail-window",
    ]
    assert all(callable(plugin.create().prepare) for plugin in plugins)
