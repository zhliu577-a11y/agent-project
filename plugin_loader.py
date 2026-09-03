# plugin_loader.py —— 插件目录解析（带 schema 校验）+ 按需连接工具
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


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def load_directory(path: str | Path) -> list[PluginEntry]:
    """读取并校验插件目录，返回 enabled 的条目。零副作用：不连接任何服务器。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    _expect(isinstance(raw, dict), f"{path}: 顶层必须是 JSON 对象")

    plugins = raw.get("plugins", [])
    _expect(isinstance(plugins, list), f"{path}: 'plugins' 必须是数组")

    entries: list[PluginEntry] = []
    for i, plugin in enumerate(plugins, start=1):
        where = f"{path}: 第 {i} 个插件"
        _expect(isinstance(plugin, dict), f"{where} 必须是对象")

        name = plugin.get("name")
        _expect(isinstance(name, str) and name.strip(), f"{where} 缺少非空 'name'")

        mcp = plugin.get("mcp")
        _expect(isinstance(mcp, dict), f"{where} ('{name}') 缺少 'mcp' 对象")
        command = mcp.get("command")
        args = mcp.get("args", [])
        _expect(
            isinstance(command, str) and command.strip(),
            f"{where} ('{name}') 的 mcp.command 必须是非空字符串",
        )
        _expect(
            isinstance(args, list) and all(isinstance(a, str) for a in args),
            f"{where} ('{name}') 的 mcp.args 必须是字符串数组",
        )

        if not plugin.get("enabled", True):
            continue  # 结构已校验；未启用的不加载

        entries.append(
            PluginEntry(
                name=name,
                description=plugin.get("description", ""),
                command=command,
                args=args,
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
