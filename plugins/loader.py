# plugins/loader.py —— 插件发现与加载（drop-in 插件目录 + kind 注册表）
#
# 每个插件是一个自包含的目录，目录内必须有 plugin.json 清单：
# {
#   "name": "time",            # 唯一名（字母/数字/下划线/连字符）
#   "type": "mcp",             # 插件类别：mcp | hook | tool | model
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
# 钩子插件 entry（type: "hook"）：
#   { "module": "hook.py", "factory": "create_hook" }
#   - 调用 factory(plugin_dir) 得到 LifecycleHooks 实例。
#
# 本地工具插件 entry（type: "tool"）：
#   { "module": "tool.py", "factory": "create_tools" }
#   - 调用 factory(plugin_dir) 得到一个 Tool 或 Tool 列表；
#     工具会被包装成 <插件名>__<工具名>，启动即注册，无需挂载。
#
# 模型插件 entry（type: "model"）：
#   { "module": "model.py", "factory": "create_model" }
#   - 只校验并持有 factory，不在装配时调用（实例化有环境变量副作用，
#     由 Harness 选定激活插件后惰性创建）。
#
# 可选字段 priority（整数，默认 0）：钩子插件的执行顺序，越小越先执行；
# 相同 priority 时按插件名排序，保证跨启动稳定。
#
# kind 注册表：新增插件类别 = 在 SUPPORTED_KINDS 登记 type，
# 实现“单个清单加载器”，再在 _KIND_HANDLERS 注册一行，装配入口自动接管。
import hashlib
import importlib.util
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.hooks import LifecycleHooks
from core.model import ModelAdapter
from core.tool import Tool

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent
SUPPORTED_KINDS = ("mcp", "hook", "tool", "model")
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
    priority: int = 0


@dataclass(frozen=True)
class McpPluginSpec:
    """已校验并解析好的 MCP 插件：可以直接交给 McpGateway 连接。"""

    manifest: PluginManifest
    transport: str  # 目前仅 "stdio"
    command: str
    args: list[str]


class NamespacedTool(Tool):
    """给本地工具包上 <插件名>__<工具名> 前缀，与 MCP 工具命名规则一致。"""

    def __init__(self, plugin_name: str, tool: Tool) -> None:
        self._plugin_name = plugin_name
        self._tool = tool

    @property
    def name(self) -> str:
        return f"{self._plugin_name}__{self._tool.name}"

    @property
    def description(self) -> str:
        return self._tool.description

    @property
    def parameters(self) -> dict[str, Any]:
        return self._tool.parameters

    async def execute(self, **kwargs: Any) -> Any:
        return await self._tool.execute(**kwargs)


@dataclass(frozen=True)
class ModelPlugin:
    """一个已校验的模型插件：清单 + 惰性工厂，由 Harness 选定后调用 create()。"""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> ModelAdapter:
        """实例化模型适配器；工厂错误或返回类型错误都会明确报错。"""
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            model = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: 模型工厂执行失败: {exc}") from exc
        _expect(
            isinstance(model, ModelAdapter),
            where,
            f"模型工厂必须返回 ModelAdapter 实例，实际是 {type(model).__name__}",
        )
        return model


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

    priority = raw.get("priority", 0)
    _expect(
        isinstance(priority, int) and not isinstance(priority, bool),
        where,
        "'priority' 必须是整数",
    )

    return PluginManifest(
        name=name,
        type=kind,
        version=version,
        description=description,
        enabled=enabled,
        directory=path.parent,
        entry=entry,
        priority=priority,
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


def _load_entry_factory(manifest: PluginManifest, kind_label: str) -> Callable[[Path], Any]:
    """解析 hook/tool 类插件的 module + factory 入口，返回工厂函数。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    entry = manifest.entry
    module_rel = entry.get("module")
    factory_name = entry.get("factory")
    _expect(
        isinstance(module_rel, str) and module_rel.strip(),
        where,
        f"{kind_label} 插件必须在 entry 里声明非空 'module'",
    )
    _expect(
        isinstance(factory_name, str) and factory_name.strip(),
        where,
        f"{kind_label} 插件必须在 entry 里声明非空 'factory'",
    )

    module_path = manifest.directory / module_rel
    _expect(module_path.is_file(), where, f"入口模块不存在: {module_path}")

    module = _import_module(manifest, module_path)
    factory = getattr(module, factory_name, None)
    _expect(callable(factory), where, f"模块 {module_rel} 中没有可调用的 '{factory_name}'")
    return factory


# ---------- 单个清单加载器：每个 kind 一个 ----------


def load_hook_plugin(manifest: PluginManifest) -> LifecycleHooks:
    """加载单个钩子插件：调用清单声明的 factory(plugin_dir) 得到钩子实例。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    factory = _load_entry_factory(manifest, "hook")
    try:
        hook = factory(manifest.directory)
    except Exception as exc:
        raise ValueError(f"{where}: 工厂执行失败: {exc}") from exc
    _expect(
        isinstance(hook, LifecycleHooks),
        where,
        f"工厂必须返回 LifecycleHooks 实例，实际是 {type(hook).__name__}",
    )
    return hook


def load_mcp_plugin(manifest: PluginManifest) -> McpPluginSpec:
    """校验并解析单个 MCP 插件，产出可直接连接的规格。"""
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
    return McpPluginSpec(manifest=manifest, transport=transport, command=command, args=args)


def _coerce_tools(value: Any, where: str) -> list[Tool]:
    """把工厂产物规范成 Tool 列表；单个 Tool、Tool 列表均合法。"""
    if isinstance(value, Tool):
        tools = [value]
    elif isinstance(value, (list, tuple)):
        tools = list(value)
    else:
        raise ValueError(f"{where}: 工厂必须返回 Tool 或 Tool 列表，实际是 {type(value).__name__}")
    for tool in tools:
        _expect(isinstance(tool, Tool), where, f"列表里混入了非 Tool 对象: {type(tool).__name__}")
    _expect(tools, where, "工厂没有返回任何 Tool")
    return tools


def load_tool_plugin(manifest: PluginManifest) -> list[Tool]:
    """加载单个本地工具插件：工厂返回的每个 Tool 都包上插件命名空间。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    factory = _load_entry_factory(manifest, "tool")
    try:
        produced = factory(manifest.directory)
    except Exception as exc:
        raise ValueError(f"{where}: 工厂执行失败: {exc}") from exc
    tools = _coerce_tools(produced, where)
    return [NamespacedTool(manifest.name, tool) for tool in tools]


def load_model_plugin(manifest: PluginManifest) -> ModelPlugin:
    """校验单个模型插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "model")
    return ModelPlugin(manifest=manifest, factory=factory)


def _resolve_arg(plugin_dir: Path, arg: str) -> str:
    """插件目录下真实存在的相对路径参数 -> 绝对路径；其余参数原样保留。"""
    path = Path(arg)
    if not path.is_absolute() and (plugin_dir / path).is_file():
        return str(plugin_dir / path)
    return arg


# ---------- 整目录加载：供单类使用与测试 ----------


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
    # 执行顺序在装配阶段就确定：priority 升序，同优先级按名字典序（稳定可预测）
    return sorted(hooks, key=lambda pair: (pair[0].priority, pair[0].name))


def load_mcp_plugins(root: str | Path | None = None) -> list[McpPluginSpec]:
    """加载插件目录里全部启用的 MCP 插件，产出可直接连接的规格。"""
    specs: list[McpPluginSpec] = []
    for manifest in discover_plugins(root):
        if manifest.type != "mcp":
            continue
        specs.append(load_mcp_plugin(manifest))
        logger.info("MCP 插件已发现: %s", manifest.name)
    return specs


def load_tool_plugins(
    root: str | Path | None = None,
) -> list[tuple[PluginManifest, list[Tool]]]:
    """加载插件目录里全部启用的本地工具插件，返回 (清单, 工具列表)。"""
    plugins: list[tuple[PluginManifest, list[Tool]]] = []
    for manifest in discover_plugins(root):
        if manifest.type != "tool":
            continue
        plugins.append((manifest, load_tool_plugin(manifest)))
        logger.info("本地工具插件已加载: %s", manifest.name)
    return sorted(plugins, key=lambda pair: pair[0].name)


def load_model_plugins(root: str | Path | None = None) -> list[ModelPlugin]:
    """加载插件目录里全部启用的模型插件，返回惰性规格（不实例化）。"""
    plugins: list[ModelPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "model":
            continue
        plugins.append(load_model_plugin(manifest))
        logger.info("模型插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


# ---------- kind 注册表与统一装配 ----------


@dataclass
class PluginAssembly:
    """一次装配的结果：按 kind 归好类的插件产物，交给对应网关/注册表。"""

    hooks: list[tuple[PluginManifest, LifecycleHooks]] = field(default_factory=list)
    mcp: list[McpPluginSpec] = field(default_factory=list)
    tools: list[tuple[PluginManifest, list[Tool]]] = field(default_factory=list)
    models: list[ModelPlugin] = field(default_factory=list)


def _add_hook(manifest: PluginManifest, assembly: PluginAssembly) -> None:
    assembly.hooks.append((manifest, load_hook_plugin(manifest)))


def _add_mcp(manifest: PluginManifest, assembly: PluginAssembly) -> None:
    assembly.mcp.append(load_mcp_plugin(manifest))


def _add_tool(manifest: PluginManifest, assembly: PluginAssembly) -> None:
    assembly.tools.append((manifest, load_tool_plugin(manifest)))


def _add_model(manifest: PluginManifest, assembly: PluginAssembly) -> None:
    assembly.models.append(load_model_plugin(manifest))


# kind 注册表：新增插件类别 = SUPPORTED_KINDS 登记 type + 这里注册一个处理器，
# 处理器把单个清单的产物放进 PluginAssembly 的对应字段。
_KIND_HANDLERS: dict[str, Callable[[PluginManifest, PluginAssembly], None]] = {
    "hook": _add_hook,
    "mcp": _add_mcp,
    "tool": _add_tool,
    "model": _add_model,
}


def assemble_plugins(root: str | Path | None = None) -> PluginAssembly:
    """扫描目录并把全部 enabled 插件按 kind 装配成 PluginAssembly。

    这是 Harness 启动器的统一装配入口；main.py 不感知具体插件类别。
    """
    assembly = PluginAssembly()
    for manifest in discover_plugins(root):
        handler = _KIND_HANDLERS.get(manifest.type)
        if handler is None:  # discover 已校验，这里是防御性兜底
            raise ValueError(f"插件类别 {manifest.type} 尚未注册加载器（{manifest.name}）")
        handler(manifest, assembly)
    assembly.hooks.sort(key=lambda pair: (pair[0].priority, pair[0].name))
    return assembly
