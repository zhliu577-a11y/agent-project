# plugins/loader.py —— 插件发现与加载（drop-in 插件目录）
#
# 每个插件是一个自包含的目录，目录内必须有 plugin.json 清单：
# {
#   "name": "time",            # 唯一名（字母/数字/下划线/连字符）
#   "type": "mcp",             # 插件类别：mcp | hook（未来可扩展）
#   "version": "1.0.0",        # 可选
#   "description": "…",        # 可选
#   "enabled": true,           # 可选，默认 true
#   "entry": { … }             # 类别相关入口
# }
#
# MCP 插件 entry：
#   { "command": "python", "args": ["server.py"], "transport": "stdio" }
#   - command == "python" 会替换为当前解释器；args 中相对于插件目录存在的
#     文件会被解析成绝对路径，其余参数原样保留。
#
# 钩子插件 entry：
#   { "module": "hook.py", "factory": "create_hook" }
#   - 从插件目录加载 module，调用 factory(plugin_dir) 得到 LifecycleHooks 实例。
import hashlib
import importlib.util
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.hooks import LifecycleHooks

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent
SUPPORTED_KINDS = ("mcp", "hook")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class PluginManifest:
    """清单的只读快照：描述插件是什么、入口在哪，不包含运行状态。"""

    name: str
    type: str
    version: str
    description: str
    enabled: bool
    directory: Path
    entry: dict[str, Any]


@dataclass(frozen=True)
class McpPluginSpec:
    """已校验并解析好的 MCP 插件：可以直接交给 McpGateway 连接。"""

    manifest: PluginManifest
    transport: str  # 目前仅 "stdio"
    command: str
    args: list[str]


def _expect(condition: bool, where: str, message: str) -> None:
    if not condition:
        raise ValueError(f"{where}: {message}")


def _parse_manifest(path: Path) -> PluginManifest:
    """读取并校验单个 plugin.json；结构错误启动即报错（不静默出错）。"""
    where = f"{path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where}: 不是合法的 JSON 文件: {exc}") from exc

    _expect(isinstance(raw, dict), where, "顶层必须是 JSON 对象")

    name = raw.get("name")
    _expect(isinstance(name, str) and _NAME_RE.fullmatch(name), where, "缺少合法的 'name'")

    kind = raw.get("type")
    _expect(
        isinstance(kind, str) and kind in SUPPORTED_KINDS,
        where,
        f"非法的 'type' '{kind}'，当前支持: {', '.join(SUPPORTED_KINDS)}",
    )

    version = raw.get("version", "")
    _expect(isinstance(version, str), where, "'version' 必须是字符串")

    description = raw.get("description", "")
    _expect(isinstance(description, str), where, "'description' 必须是字符串")

    enabled = raw.get("enabled", True)
    _expect(isinstance(enabled, bool), where, "'enabled' 必须是布尔值")

    entry = raw.get("entry")
    _expect(isinstance(entry, dict), where, "缺少 'entry' 对象")

    return PluginManifest(
        name=name,
        type=kind,
        version=version,
        description=description,
        enabled=enabled,
        directory=path.parent,
        entry=entry,
    )


def _iter_manifest_paths(root: Path):
    """递归寻找 plugin.json；找到的插件目录不再向下钻取（插件自身即叶子）。"""
    stack = [root]
    while stack:
        directory = stack.pop()
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            if child.name.startswith((".", "__")):
                continue
            manifest_path = child / "plugin.json"
            if manifest_path.is_file():
                yield manifest_path
            else:
                stack.append(child)


def discover_plugins(root: str | Path | None = None) -> list[PluginManifest]:
    """扫描插件目录，返回 enabled 的插件清单（零副作用：不导入、不连接）。

    结构仍会校验：即便 disabled，写错的清单也会抛 ValueError，绝不静默出错。
    """
    root = Path(root) if root is not None else DEFAULT_PLUGINS_DIR
    if not root.exists():
        logger.warning("插件目录不存在，跳过: %s", root)
        return []

    manifests: list[PluginManifest] = []
    for manifest_path in _iter_manifest_paths(root):
        manifest = _parse_manifest(manifest_path)
        if manifest.enabled:
            manifests.append(manifest)

    seen: set[tuple[str, str]] = set()
    for manifest in manifests:
        key = (manifest.type, manifest.name)
        if key in seen:
            raise ValueError(f"插件重名（{manifest.type}/{manifest.name}）: {manifest.directory}")
        seen.add(key)

    return sorted(manifests, key=lambda m: (m.type, m.name))


def _import_module(manifest: PluginManifest, module_path: Path):
    """把插件目录里的一个 Python 文件作为独立模块加载（插件彼此不共享符号）。"""
    digest = hashlib.sha1(str(module_path.resolve()).encode("utf-8")).hexdigest()[:10]
    module_name = f"_agent_plugin_{manifest.type}_{manifest.name}_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ValueError(f"插件 {manifest.name} 无法作为模块加载: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ValueError(f"插件 {manifest.name} 代码执行失败: {exc}") from exc
    return module


def load_hook_plugin(manifest: PluginManifest) -> LifecycleHooks:
    """加载单个钩子插件：调用清单声明的 factory(plugin_dir) 得到钩子实例。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    entry = manifest.entry
    module_rel = entry.get("module")
    factory_name = entry.get("factory")
    _expect(
        isinstance(module_rel, str) and module_rel.strip(),
        where,
        "hook 插件必须在 entry 里声明非空 'module'",
    )
    _expect(
        isinstance(factory_name, str) and factory_name.strip(),
        where,
        "hook 插件必须在 entry 里声明非空 'factory'",
    )

    module_path = manifest.directory / module_rel
    _expect(module_path.is_file(), where, f"入口模块不存在: {module_path}")

    module = _import_module(manifest, module_path)
    factory = getattr(module, factory_name, None)
    _expect(callable(factory), where, f"模块 {module_rel} 中没有可调用的 '{factory_name}'")

    try:
        hook = factory(manifest.directory)
    except Exception as exc:
        raise ValueError(f"{where}: 工厂 {factory_name} 执行失败: {exc}") from exc
    _expect(
        isinstance(hook, LifecycleHooks),
        where,
        f"工厂 {factory_name} 必须返回 LifecycleHooks 实例，实际是 {type(hook).__name__}",
    )
    return hook


def load_hook_plugins(
    root: str | Path | None = None,
) -> list[tuple[PluginManifest, LifecycleHooks]]:
    """加载插件目录里全部启用的钩子插件，返回 (清单, 实例) 列表。"""
    hooks: list[tuple[PluginManifest, LifecycleHooks]] = []
    for manifest in discover_plugins(root):
        if manifest.type != "hook":
            continue
        hooks.append((manifest, load_hook_plugin(manifest)))
        logger.info("钩子插件已加载: %s", manifest.name)
    return hooks


def _resolve_arg(plugin_dir: Path, arg: str) -> str:
    """插件目录下真实存在的相对路径参数 -> 绝对路径；其余参数原样保留。"""
    path = Path(arg)
    if not path.is_absolute() and (plugin_dir / path).is_file():
        return str(plugin_dir / path)
    return arg


def load_mcp_plugins(root: str | Path | None = None) -> list[McpPluginSpec]:
    """加载插件目录里全部启用的 MCP 插件，产出可直接连接的规格。"""
    specs: list[McpPluginSpec] = []
    for manifest in discover_plugins(root):
        if manifest.type != "mcp":
            continue
        where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
        entry = manifest.entry

        transport = entry.get("transport", "stdio")
        _expect(transport == "stdio", where, f"暂不支持 '{transport}' transport，当前仅支持 stdio")

        command = entry.get("command")
        _expect(isinstance(command, str) and command.strip(), where, "缺少非空 'command'")

        args = entry.get("args", [])
        _expect(
            isinstance(args, list) and all(isinstance(a, str) for a in args),
            where,
            "'args' 必须是字符串数组",
        )

        command = sys.executable if command == "python" else command
        args = [_resolve_arg(manifest.directory, arg) for arg in args]
        specs.append(
            McpPluginSpec(manifest=manifest, transport=transport, command=command, args=args)
        )
        logger.info("MCP 插件已发现: %s", manifest.name)
    return specs
