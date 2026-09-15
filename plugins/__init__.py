# plugins —— 插件子系统：一个"拖入即用"的插件目录。
#
# 目录约定：
#   plugins/
#     mcp/<name>/    MCP 工具插件（plugin.json + 服务器代码/配置）
#     hooks/<name>/  生命周期钩子插件（plugin.json + Python 实现）
#
# 单插件和功能包都会展开为 contribution。新增插件 kind 时，由受信任的
# 核心代码调用 register_kind()，插件清单不能自行注册执行阶段。
from plugins.loader import (
    DEFAULT_PLUGINS_DIR,
    DeclaredError,
    EmbeddingPlugin,
    KindHandler,
    ListenerPlugin,
    McpPluginSpec,
    MemoryPlugin,
    ModelPlugin,
    NamespacedTool,
    PackageInspection,
    PluginAssembly,
    PluginContribution,
    PluginManifest,
    SessionPlugin,
    SkillPlugin,
    assemble_plugins,
    attach_listener_plugins,
    discover_contributions,
    discover_plugins,
    inspect_package,
    load_embedding_plugin,
    load_embedding_plugins,
    load_hook_plugin,
    load_hook_plugins,
    load_listener_plugin,
    load_listener_plugins,
    load_mcp_plugins,
    load_memory_plugin,
    load_memory_plugins,
    load_model_plugin,
    load_model_plugins,
    load_session_plugin,
    load_session_plugins,
    load_skill_plugin,
    load_skill_plugins,
    load_tool_plugin,
    load_tool_plugins,
    register_kind,
    registered_kinds,
)
from plugins.manager import PluginManager
from plugins.registry import PluginRecord, PluginRegistry

__all__ = [
    "DEFAULT_PLUGINS_DIR",
    "SUPPORTED_KINDS",
    "DeclaredError",
    "EmbeddingPlugin",
    "KindHandler",
    "ListenerPlugin",
    "McpPluginSpec",
    "MemoryPlugin",
    "ModelPlugin",
    "NamespacedTool",
    "PackageInspection",
    "PluginAssembly",
    "PluginContribution",
    "PluginManifest",
    "PluginManager",
    "PluginRecord",
    "PluginRegistry",
    "SessionPlugin",
    "SkillPlugin",
    "assemble_plugins",
    "attach_listener_plugins",
    "discover_contributions",
    "discover_plugins",
    "inspect_package",
    "load_embedding_plugin",
    "load_embedding_plugins",
    "load_hook_plugin",
    "load_hook_plugins",
    "load_listener_plugin",
    "load_listener_plugins",
    "load_memory_plugin",
    "load_memory_plugins",
    "load_mcp_plugins",
    "load_model_plugin",
    "load_model_plugins",
    "load_session_plugin",
    "load_session_plugins",
    "load_skill_plugin",
    "load_skill_plugins",
    "load_tool_plugin",
    "load_tool_plugins",
    "register_kind",
    "registered_kinds",
]


def __getattr__(name: str):
    if name == "SUPPORTED_KINDS":
        return registered_kinds()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
