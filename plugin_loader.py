# plugin_loader.py —— 插件目录解析 + 按需连接工具
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.registry import ToolRegistry
from core.tool import Tool
from mcp_bridge import McpBridge, register_mcp_server


@dataclass
class PluginEntry:
    """目录里的一个插件条目（只读清单，不涉及连接）。"""

    name: str
    description: str
    command: str
    args: list[str]


def load_directory(path: str | Path) -> list[PluginEntry]:
    """读取插件目录，返回 enabled 的条目。零副作用：不连接任何服务器。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries: list[PluginEntry] = []
    for plugin in raw.get("plugins", []):
        if not plugin.get("enabled", True):
            continue
        mcp = plugin.get("mcp", {})
        entries.append(
            PluginEntry(
                name=plugin["name"],
                description=plugin.get("description", ""),
                command=mcp.get("command", "python"),
                args=mcp.get("args", []),
            )
        )
    return entries


class UseServer(Tool):
    """把"按需加载服务器"本身做成一个工具：模型决定需要哪个服务器时调用它。"""

    name = "use_server"
    description = "按需加载指定的 MCP 服务器并注册其工具；加载成功后才能使用该服务器的工具。"

    def __init__(
        self,
        bridge: McpBridge,
        registry: ToolRegistry,
        entries: list[PluginEntry],
        python: str,
    ) -> None:
        self._bridge = bridge
        self._registry = registry
        self._entries = {e.name: e for e in entries}
        self._loaded: set[str] = set()
        self._python = python

    @property
    def parameters(self) -> dict[str, Any]:
        available = "、".join(self._entries) if self._entries else "（无可用服务器）"
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": f"要加载的服务器名，可选：{available}",
                }
            },
            "required": ["name"],
        }

    async def execute(self, **kwargs: Any) -> str:
        name = kwargs["name"]
        entry = self._entries.get(name)
        if entry is None:
            return f"未知服务器: {name}，可用: {list(self._entries)}"
        if name in self._loaded:
            return f"服务器 {name} 已加载，无需重复加载"

        command = self._python if entry.command == "python" else entry.command
        try:
            tool_names = await register_mcp_server(
                self._registry, self._bridge, command, entry.args
            )
        except Exception as exc:
            return f"服务器 {name} 加载失败: {exc}"

        self._loaded.add(name)
        return f"服务器 {name} 已加载工具: {tool_names}"
