# plugins —— 插件子系统：一个"拖入即用"的插件目录。
#
# 目录约定：
#   plugins/
#     mcp/<name>/    MCP 工具插件（plugin.json + 服务器代码/配置）
#     hooks/<name>/  生命周期钩子插件（plugin.json + Python 实现）
#
# 后续新增插件类别（skills / models / …）时，在 plugins/loader.py 的
# SUPPORTED_KINDS 中登记即可，内核与网关边界不变。
from plugins.loader import (
    DEFAULT_PLUGINS_DIR,
    McpPluginSpec,
    PluginManifest,
    discover_plugins,
    load_hook_plugin,
    load_hook_plugins,
    load_mcp_plugins,
)

__all__ = [
    "DEFAULT_PLUGINS_DIR",
    "McpPluginSpec",
    "PluginManifest",
    "discover_plugins",
    "load_hook_plugin",
    "load_hook_plugins",
    "load_mcp_plugins",
]
