# plugins/loader.py —— 插件发现与加载（drop-in 插件目录 + kind 注册表）
#
# 每个插件是一个自包含的目录，目录内必须有 plugin.json 清单。
# 单插件清单使用 type + entry：
# {
#   "name": "time",            # 唯一名（字母/数字/下划线/连字符）
#   "type": "mcp",             # 插件类别：受 KindHandler 注册表控制
#   "version": "1.0.0",        # 可选
#   "description": "…",        # 可选
#   "enabled": true,           # 可选，默认 true
#   "entry": { … }             # 类别相关入口
# }
#
# 功能包清单使用 apiVersion + contributes[]，一次声明多个能力：
# {
#   "apiVersion": "1",
#   "name": "quality",
#   "contributes": [
#     { "id": "lint", "kind": "skill",
#       "entry": { "content": "skills/lint/SKILL.md" } },
#     { "id": "text-tools", "kind": "tool",
#       "entry": { "module": "tool.py", "factory": "create_tools" } }
#   ]
# }
#
# 两种清单在发现阶段统一展开为 PluginContribution；装配阶段只按 kind
# 分派给受信任代码注册的 KindHandler。
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
# 工具插件 entry（type: "tool"）：
#   Python 进程内：
#     { "runtime": "python", "module": "tool.py", "factory": "create_tools" }
#   外部进程（可由 Node/TypeScript 等实现）：
#     { "runtime": "node", "protocol": "jsonrpc-stdio",
#       "command": ["node", "dist/index.js"], "tools": [ ... ] }
#   - 工具会被包装成 <插件名>__<工具名>，启动即注册，无需挂载；
#   - 外部进程首次调用时启动，Runtime 关闭时统一回收。
#
# 模型插件 entry（type: "model"）：
#   { "module": "model.py", "factory": "create_model" }
#   - 只校验并持有 factory，不在装配时调用（实例化有环境变量副作用，
#     由 Harness 选定激活插件后惰性创建）。
#
# 技能插件 entry（type: "skill"，纯内容，不执行代码）：
#   { "content": "SKILL.md", "preload": false }
#   - content 为正文文件（相对插件目录，默认 SKILL.md），装配时校验存在；
#   - preload=false（默认）表示正文只在模型调用 use_skill 时读取（渐进披露）；
#     preload=true 表示启动时把正文注入系统提示词（仅限全局规则，尽量少用）。
#
# 可选字段 priority（整数，默认 0）：钩子插件的执行顺序，越小越先执行；
# 相同 priority 时按插件名排序，保证跨启动稳定。
#
# kind 注册表：核心代码显式注册 KindHandler；manifest 只能引用已经注册的
# kind，不能通过配置新增核心执行阶段。
import hashlib
import importlib.util
import json
import logging
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.context import ContextPolicy
from core.embedding import EmbeddingProvider
from core.errors import AGENT_CATEGORIES, resolve_declared_error
from core.events import EventBus, Subscription, coerce_subscriptions
from core.hooks import LifecycleHooks
from core.memory import MemoryStore
from core.model import ModelAdapter
from core.session import SessionStore
from core.tool import Tool
from plugins.external import ExternalTool, ExternalToolDefinition, StdioJsonRpcHost

logger = logging.getLogger(__name__)

DEFAULT_PLUGINS_DIR = Path(__file__).resolve().parent
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_EVENT_RE = re.compile(r"^(\*|[A-Za-z0-9_.-]+)$")
_EXTERNAL_RUNTIMES = frozenset({"node", "process"})


@dataclass(frozen=True)
class DeclaredError:
    """插件在清单里声明的领域错误（code/category/hint/retryable）。"""

    code: str
    category: str
    hint: str
    retryable: bool


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
    errors: tuple[DeclaredError, ...] = ()
    events: tuple[str, ...] = ()
    package_name: str | None = None
    contribution_id: str | None = None


@dataclass(frozen=True)
class PluginContribution:
    """A normalized capability unit produced by either a package or a single plugin."""

    package_name: str
    contribution_id: str
    manifest: PluginManifest

    @property
    def kind(self) -> str:
        return self.manifest.type


@dataclass(frozen=True)
class PackageInspection:
    """静态检查后的插件包；不会导入或执行插件代码。"""

    name: str
    version: str
    description: str
    directory: Path
    manifests: tuple[PluginManifest, ...]

    @property
    def contributions(self) -> tuple[PluginContribution, ...]:
        return tuple(
            PluginContribution(
                package_name=manifest.package_name or manifest.name,
                contribution_id=manifest.contribution_id or "default",
                manifest=manifest,
            )
            for manifest in self.manifests
        )


@dataclass(frozen=True)
class KindHandler:
    """Trusted load/apply contract for one contribution kind."""

    kind: str
    load: Callable[[PluginManifest], Any]
    apply: Callable[["PluginAssembly", PluginManifest, Any], None]


_KIND_REGISTRY: dict[str, KindHandler] = {}


def register_kind(handler: KindHandler, *, replace: bool = False) -> None:
    """Register a trusted kind handler during harness startup."""
    global SUPPORTED_KINDS
    if not _NAME_RE.fullmatch(handler.kind):
        raise ValueError(f"invalid kind name: {handler.kind!r}")
    if handler.kind in _KIND_REGISTRY and not replace:
        raise ValueError(f"kind already registered: {handler.kind}")
    _KIND_REGISTRY[handler.kind] = handler
    SUPPORTED_KINDS = registered_kinds()


def registered_kinds() -> tuple[str, ...]:
    return tuple(_KIND_REGISTRY)


@dataclass(frozen=True)
class McpPluginSpec:
    """已校验并解析好的 MCP 插件：可以直接交给 McpGateway 连接。"""

    manifest: PluginManifest
    transport: str  # 目前仅 "stdio"
    command: str
    args: list[str]


@dataclass(frozen=True)
class StdioToolSpec:
    """Validated external tool process configuration."""

    manifest: PluginManifest
    protocol: str
    command: str
    args: list[str]
    timeout: float
    tools: tuple[ExternalToolDefinition, ...]


class NamespacedTool(Tool):
    """给本地工具包上 <插件名>__<工具名> 前缀，并按其声明补全错误语义。"""

    def __init__(
        self,
        plugin_name: str,
        tool: Tool,
        declarations: dict[str, DeclaredError] | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._tool = tool
        self._declarations = declarations or {}

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
        try:
            return await self._tool.execute(**kwargs)
        except Exception as exc:
            resolved = resolve_declared_error(exc, self._declarations, plugin=self._plugin_name)
            if resolved is exc:
                raise
            raise resolved from exc

    async def close(self) -> None:
        """Delegate lifecycle cleanup when the wrapped tool owns a process."""
        close = getattr(self._tool, "close", None)
        if callable(close):
            await close()


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


@dataclass(frozen=True)
class ContextPlugin:
    """A context policy plugin selected by the runtime for each conversation."""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> ContextPolicy:
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            policy = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: context factory failed: {exc}") from exc
        _expect(
            isinstance(policy, ContextPolicy),
            where,
            "context factory must return a ContextPolicy with an async prepare() method, "
            f"got {type(policy).__name__}",
        )
        return policy


@dataclass(frozen=True)
class SkillPlugin:
    """技能插件：纯内容目录（不执行代码），正文文件由网关惰性读取。"""

    manifest: PluginManifest
    content_path: Path
    preload: bool


@dataclass(frozen=True)
class SessionPlugin:
    """会话存储插件：清单 + 惰性工厂，由 Harness 选定 SESSION_STORE 后创建。"""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> SessionStore:
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            store = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: 会话存储工厂执行失败: {exc}") from exc
        _expect(
            isinstance(store, SessionStore),
            where,
            f"会话存储工厂必须返回 SessionStore 实例，实际是 {type(store).__name__}",
        )
        return store


@dataclass(frozen=True)
class MemoryPlugin:
    """长期记忆存储插件：清单 + 惰性工厂，由 Harness 选定 MEMORY_STORE 后创建。"""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> MemoryStore:
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            store = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: 记忆存储工厂执行失败: {exc}") from exc
        _expect(
            isinstance(store, MemoryStore),
            where,
            f"记忆存储工厂必须返回 MemoryStore 实例，实际是 {type(store).__name__}",
        )
        return store


@dataclass(frozen=True)
class EmbeddingPlugin:
    """嵌入提供方插件：清单 + 惰性工厂，由 EMBEDDING_PROVIDER 选定后创建。"""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> EmbeddingProvider:
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            provider = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: 嵌入提供方工厂执行失败: {exc}") from exc
        _expect(
            isinstance(provider, EmbeddingProvider),
            where,
            f"嵌入提供方工厂必须返回 EmbeddingProvider 实例，实际是 {type(provider).__name__}",
        )
        return provider


@dataclass(frozen=True)
class ListenerPlugin:
    """事件订阅者插件：清单 + 惰性工厂，返回 Subscription 列表接入事件总线。"""

    manifest: PluginManifest
    factory: Callable[[Path], Any]

    def create(self) -> tuple[Subscription, ...]:
        where = f"{self.manifest.directory / 'plugin.json'} ('{self.manifest.name}')"
        try:
            value = self.factory(self.manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: listener 工厂执行失败: {exc}") from exc
        subscriptions = coerce_subscriptions(value, where)
        if self.manifest.events:
            declared = set(self.manifest.events)
            for subscription in subscriptions:
                if subscription.event not in declared:
                    raise ValueError(
                        f"{where}: 订阅了未声明的事件 '{subscription.event}'，"
                        f"清单声明: {sorted(declared)}"
                    )
        return subscriptions


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

    errors = _parse_declared_errors(raw, where)
    events = _parse_declared_events(raw, where)

    return PluginManifest(
        name=name,
        type=kind,
        version=version,
        description=description,
        enabled=enabled,
        directory=path.parent,
        entry=entry,
        priority=priority,
        errors=errors,
        events=events,
    )


def _parse_declared_events(raw: dict[str, Any], where: str) -> tuple[str, ...]:
    """校验 listener 插件的 events 声明（事件名列表，支持 "*"）。"""
    items = raw.get("events", [])
    _expect(isinstance(items, list), where, "'events' 必须是数组")
    declared: list[str] = []
    for index, item in enumerate(items, start=1):
        item_where = f"{where} 第 {index} 个事件声明"
        _expect(
            isinstance(item, str) and bool(_EVENT_RE.fullmatch(item)),
            item_where,
            "事件名必须形如 'tool.after'，或为 '*'",
        )
        _expect(item not in declared, item_where, f"事件重复声明: {item}")
        declared.append(item)
    return tuple(declared)


def _parse_declared_errors(raw: dict[str, Any], where: str) -> tuple[DeclaredError, ...]:
    """校验清单里的 errors 声明：code 唯一、category 合法、retryable 自洽。"""
    items = raw.get("errors", [])
    _expect(isinstance(items, list), where, "'errors' 必须是数组")

    declared: list[DeclaredError] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        item_where = f"{where} 第 {index} 个错误声明"
        _expect(isinstance(item, dict), item_where, "必须是对象")

        code = item.get("code")
        _expect(
            isinstance(code, str) and bool(_CODE_RE.fullmatch(code)),
            item_where,
            "缺少合法的 'code'（仅 A-Za-z0-9_.-）",
        )
        _expect(code not in seen, item_where, f"错误码重复: {code}")

        category = item.get("category", "plugin")
        _expect(
            category in AGENT_CATEGORIES,
            item_where,
            f"非法的 category '{category}'，可选: {list(AGENT_CATEGORIES)}",
        )

        hint = item.get("hint", "")
        _expect(isinstance(hint, str), item_where, "'hint' 必须是字符串")

        retryable = item.get("retryable", category == "retryable")
        _expect(isinstance(retryable, bool), item_where, "'retryable' 必须是布尔值")
        _expect(
            retryable == (category == "retryable"),
            item_where,
            "'retryable' 必须与 category == 'retryable' 一致",
        )

        declared.append(DeclaredError(code=code, category=category, hint=hint, retryable=retryable))
        seen.add(code)
    return tuple(declared)


def _read_manifest(path: Path) -> dict[str, Any]:
    where = f"{path}"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{where}: invalid JSON manifest: {exc}") from exc
    _expect(isinstance(raw, dict), where, "manifest root must be a JSON object")
    return raw


def _merge_declared_errors(
    defaults: tuple[DeclaredError, ...],
    extra: tuple[DeclaredError, ...],
    where: str,
) -> tuple[DeclaredError, ...]:
    merged = list(defaults)
    seen = {error.code for error in defaults}
    for error in extra:
        _expect(error.code not in seen, where, f"duplicate error code: {error.code}")
        merged.append(error)
        seen.add(error.code)
    return tuple(merged)


def _merge_declared_events(
    defaults: tuple[str, ...],
    extra: tuple[str, ...],
    where: str,
) -> tuple[str, ...]:
    merged = list(defaults)
    seen = set(defaults)
    for event in extra:
        _expect(event not in seen, where, f"duplicate event declaration: {event}")
        merged.append(event)
        seen.add(event)
    return tuple(merged)


def _parse_package_contributions(path: Path, raw: dict[str, Any]) -> list[PluginManifest]:
    """Expand a package manifest into one normalized manifest per contribution."""
    where = f"{path}"
    _expect(raw.get("apiVersion") == "1", where, "'apiVersion' must be '1'")
    _expect("type" not in raw, where, "manifest cannot contain both 'type' and 'contributes'")

    package_name = raw.get("name")
    _expect(
        isinstance(package_name, str) and _NAME_RE.fullmatch(package_name),
        where,
        "missing valid 'name'",
    )

    version = raw.get("version", "")
    _expect(isinstance(version, str), where, "'version' must be a string")

    description = raw.get("description", "")
    _expect(isinstance(description, str), where, "'description' must be a string")

    package_enabled = raw.get("enabled", True)
    _expect(isinstance(package_enabled, bool), where, "'enabled' must be bool")

    default_priority = raw.get("priority", 0)
    _expect(
        isinstance(default_priority, int) and not isinstance(default_priority, bool),
        where,
        "'priority' must be an integer",
    )

    package_errors = _parse_declared_errors(raw, where)
    package_events = _parse_declared_events(raw, where)
    items = raw.get("contributes")
    _expect(
        isinstance(items, list) and bool(items),
        where,
        "'contributes' must be a non-empty array",
    )

    contributions: list[PluginManifest] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items, start=1):
        item_where = f"{where} contributes[{index}]"
        _expect(isinstance(item, dict), item_where, "contribution must be an object")

        contribution_id = item.get("id")
        _expect(
            isinstance(contribution_id, str) and _NAME_RE.fullmatch(contribution_id),
            item_where,
            "missing valid 'id'",
        )
        _expect(
            contribution_id not in seen_ids,
            item_where,
            f"duplicate contribution id: {contribution_id}",
        )

        kind = item.get("kind")
        _expect(
            isinstance(kind, str) and kind in _KIND_REGISTRY,
            item_where,
            f"invalid 'kind' {kind!r}; supported: {', '.join(registered_kinds())}",
        )

        entry = item.get("entry")
        _expect(isinstance(entry, dict), item_where, "missing 'entry' object")

        item_version = item.get("version", version)
        _expect(isinstance(item_version, str), item_where, "'version' must be a string")

        item_description = item.get("description", description)
        _expect(
            isinstance(item_description, str),
            item_where,
            "'description' must be a string",
        )

        item_enabled = item.get("enabled", True)
        _expect(isinstance(item_enabled, bool), item_where, "'enabled' must be bool")

        item_priority = item.get("priority", default_priority)
        _expect(
            isinstance(item_priority, int) and not isinstance(item_priority, bool),
            item_where,
            "'priority' must be an integer",
        )

        item_errors = _parse_declared_errors(item, item_where)
        errors = _merge_declared_errors(package_errors, item_errors, item_where)
        item_events = _parse_declared_events(item, item_where)
        events = _merge_declared_events(package_events, item_events, item_where)
        contributions.append(
            PluginManifest(
                name=f"{package_name}--{contribution_id}",
                type=kind,
                version=item_version,
                description=item_description,
                enabled=package_enabled and item_enabled,
                directory=path.parent,
                entry=entry,
                priority=item_priority,
                errors=errors,
                events=events,
                package_name=package_name,
                contribution_id=contribution_id,
            )
        )
        seen_ids.add(contribution_id)
    return contributions


def _iter_manifest_paths(root: Path):
    """递归寻找 plugin.json；找到的插件目录不再向下钻取（插件自身即叶子）。"""
    root_manifest = root / "plugin.json"
    if root_manifest.is_file():
        yield root_manifest
        return

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


def discover_contributions(root: str | Path | None = None) -> list[PluginContribution]:
    """扫描插件目录，把单插件和功能包统一展开为 enabled contribution。

    discovery 只读磁盘，不导入代码、不连接服务。结构仍会校验：即便
    disabled，写错的清单也会抛 ValueError，绝不静默出错。
    """
    roots = _normalize_roots(root)
    contributions: list[PluginContribution] = []
    package_directories: dict[str, Path] = {}
    for plugin_root in roots:
        if not plugin_root.exists():
            logger.warning("插件目录不存在，跳过: %s", plugin_root)
            continue
        for manifest_path in _iter_manifest_paths(plugin_root):
            raw = _read_manifest(manifest_path)
            manifests = (
                _parse_package_contributions(manifest_path, raw)
                if "contributes" in raw
                else [_parse_manifest(manifest_path)]
            )
            for manifest in manifests:
                if manifest.package_name is not None:
                    previous = package_directories.get(manifest.package_name)
                    if previous is not None and previous != manifest.directory:
                        raise ValueError(
                            f"功能包重名（{manifest.package_name}）: "
                            f"{previous} / {manifest.directory}"
                        )
                    package_directories[manifest.package_name] = manifest.directory
                if manifest.enabled:
                    contributions.append(
                        PluginContribution(
                            package_name=manifest.package_name or manifest.name,
                            contribution_id=manifest.contribution_id or "default",
                            manifest=manifest,
                        )
                    )

    seen: set[tuple[str, str]] = set()
    for contribution in contributions:
        key = (contribution.kind, contribution.manifest.name)
        if key in seen:
            manifest = contribution.manifest
            raise ValueError(f"插件重名（{manifest.type}/{manifest.name}）: {manifest.directory}")
        seen.add(key)

    return sorted(
        contributions,
        key=lambda c: (c.kind, c.manifest.name, c.package_name, c.contribution_id),
    )


def _normalize_roots(root: str | Path | Sequence[str | Path] | None) -> list[Path]:
    if root is None:
        return [DEFAULT_PLUGINS_DIR]
    if isinstance(root, (str, Path)):
        return [Path(root)]
    return [Path(item) for item in root]


def inspect_package(root: str | Path) -> PackageInspection:
    """静态读取并校验一个插件目录，不导入任何插件代码。"""
    package_dir = Path(root).resolve()
    manifest_path = package_dir / "plugin.json"
    raw = _read_manifest(manifest_path)
    if "contributes" in raw:
        manifests = tuple(_parse_package_contributions(manifest_path, raw))
        name = raw["name"]
        version = raw.get("version", "")
        description = raw.get("description", "")
    else:
        manifest = _parse_manifest(manifest_path)
        manifests = (manifest,)
        name = manifest.name
        version = manifest.version
        description = manifest.description

    for manifest in manifests:
        _validate_static_entry(manifest)
    return PackageInspection(
        name=name,
        version=version,
        description=description,
        directory=package_dir,
        manifests=manifests,
    )


def _validate_static_entry(manifest: PluginManifest) -> None:
    """校验入口文件存在且位于插件目录内，但不导入模块、不执行工厂。"""
    if manifest.type == "tool":
        runtime = manifest.entry.get("runtime", "python")
        if runtime in _EXTERNAL_RUNTIMES:
            _parse_stdio_tool_spec(manifest)
            return
        _expect(
            runtime == "python",
            f"{manifest.directory / 'plugin.json'} ('{manifest.name}')",
            f"不支持的 tool runtime '{runtime}'；可选: python, node, process",
        )

    if manifest.type in {
        "hook",
        "tool",
        "model",
        "context",
        "session",
        "memory",
        "embedding",
        "listener",
    }:
        _validate_python_module_entry(manifest)
    elif manifest.type == "skill":
        where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
        entry = manifest.entry
        content_rel = entry.get("content", "SKILL.md")
        _expect(
            isinstance(content_rel, str) and content_rel.strip(),
            where,
            "skill 插件必须在 entry 里声明非空 'content'",
        )
        content_path = _resolve_inside(manifest.directory, content_rel, where)
        _expect(content_path.is_file(), where, f"正文文件不存在: {content_path}")
    elif manifest.type == "mcp":
        where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
        entry = manifest.entry
        transport = entry.get("transport", "stdio")
        _expect(transport == "stdio", where, f"不支持的 transport: {transport}")
        command = entry.get("command")
        _expect(isinstance(command, str) and command.strip(), where, "缺少非空 'command'")
        args = entry.get("args", [])
        _expect(
            isinstance(args, list) and all(isinstance(item, str) for item in args),
            where,
            "'args' 必须是字符串数组",
        )


def _validate_python_module_entry(manifest: PluginManifest) -> None:
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    entry = manifest.entry
    runtime = entry.get("runtime", "python")
    _expect(
        runtime == "python",
        where,
        f"{manifest.type} 插件的进程内入口只支持 runtime 'python'，实际是 '{runtime}'",
    )
    module_rel = entry.get("module")
    factory_name = entry.get("factory")
    _expect(
        isinstance(module_rel, str) and module_rel.strip(),
        where,
        f"{manifest.type} 插件必须在 entry 里声明非空 'module'",
    )
    _expect(
        isinstance(factory_name, str) and factory_name.strip(),
        where,
        f"{manifest.type} 插件必须在 entry 里声明非空 'factory'",
    )
    module_path = _resolve_inside(manifest.directory, module_rel, where)
    _expect(module_path.is_file(), where, f"入口模块不存在: {module_path}")


def _parse_stdio_tool_spec(manifest: PluginManifest) -> StdioToolSpec:
    """Parse a static external tool catalog and process launch configuration."""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    entry = manifest.entry

    runtime = entry.get("runtime")
    _expect(
        runtime in _EXTERNAL_RUNTIMES,
        where,
        f"外部 tool runtime 必须是 {sorted(_EXTERNAL_RUNTIMES)} 之一",
    )
    protocol = entry.get("protocol")
    _expect(
        protocol == "jsonrpc-stdio",
        where,
        "外部 tool 目前只支持 protocol 'jsonrpc-stdio'",
    )

    raw_command = entry.get("command")
    raw_args = entry.get("args", [])
    _expect(
        isinstance(raw_args, list) and all(isinstance(arg, str) for arg in raw_args),
        where,
        "'args' 必须是字符串数组",
    )
    if isinstance(raw_command, str):
        command_parts = [raw_command, *raw_args]
    elif isinstance(raw_command, list):
        _expect(
            all(isinstance(part, str) for part in raw_command),
            where,
            "'command' 数组必须全部是字符串",
        )
        command_parts = [*raw_command, *raw_args]
    else:
        raise ValueError(f"{where}: 外部 tool 必须声明字符串或字符串数组 'command'")

    _expect(bool(command_parts), where, "'command' 不能为空")
    command = sys.executable if command_parts[0] == "python" else command_parts[0]
    args = [_resolve_arg(manifest.directory, arg) for arg in command_parts[1:]]

    timeout = entry.get("timeout", 30.0)
    _expect(
        isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0,
        where,
        "'timeout' 必须是正数",
    )

    raw_tools = entry.get("tools")
    _expect(
        isinstance(raw_tools, list) and bool(raw_tools),
        where,
        "外部 tool 必须在 entry.tools 声明非空工具目录",
    )
    definitions: list[ExternalToolDefinition] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_tools, start=1):
        item_where = f"{where} tools[{index}]"
        _expect(isinstance(item, dict), item_where, "工具声明必须是对象")

        name = item.get("name")
        _expect(
            isinstance(name, str) and bool(_NAME_RE.fullmatch(name)),
            item_where,
            "缺少合法的 'name'",
        )
        _expect(name not in seen_names, item_where, f"工具名重复: {name}")

        description = item.get("description", "")
        _expect(isinstance(description, str), item_where, "'description' 必须是字符串")

        parameters = item.get("parameters", {"type": "object", "properties": {}})
        _expect(isinstance(parameters, dict), item_where, "'parameters' 必须是 JSON Schema 对象")

        definitions.append(
            ExternalToolDefinition(
                name=name,
                description=description,
                parameters=parameters,
            )
        )
        seen_names.add(name)

    return StdioToolSpec(
        manifest=manifest,
        protocol=protocol,
        command=command,
        args=args,
        timeout=float(timeout),
        tools=tuple(definitions),
    )


def _resolve_inside(base: Path, relative: str, where: str) -> Path:
    path = (base / relative).resolve()
    try:
        path.relative_to(base.resolve())
    except ValueError as exc:
        raise ValueError(f"{where}: 入口路径越出插件目录: {relative}") from exc
    return path


def discover_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[PluginManifest]:
    """Backward-compatible projection: return normalized contribution manifests."""
    return [contribution.manifest for contribution in discover_contributions(root)]


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
    runtime = entry.get("runtime", "python")
    _expect(
        runtime == "python",
        where,
        f"{kind_label} 插件的进程内入口只支持 runtime 'python'，实际是 '{runtime}'",
    )
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
    """加载单个工具插件，并把每个工具包上插件命名空间。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    runtime = manifest.entry.get("runtime", "python")
    if runtime in _EXTERNAL_RUNTIMES:
        spec = _parse_stdio_tool_spec(manifest)
        host = StdioJsonRpcHost(
            plugin_name=manifest.name,
            plugin_dir=manifest.directory,
            command=spec.command,
            args=spec.args,
            timeout=spec.timeout,
        )
        tools: list[Tool] = [ExternalTool(host, definition) for definition in spec.tools]
    else:
        factory = _load_entry_factory(manifest, "tool")
        try:
            produced = factory(manifest.directory)
        except Exception as exc:
            raise ValueError(f"{where}: 工厂执行失败: {exc}") from exc
        tools = _coerce_tools(produced, where)
    declarations = {declared.code: declared for declared in manifest.errors}
    return [NamespacedTool(manifest.name, tool, declarations=declarations) for tool in tools]


def load_model_plugin(manifest: PluginManifest) -> ModelPlugin:
    """校验单个模型插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "model")
    return ModelPlugin(manifest=manifest, factory=factory)


def load_context_plugin(manifest: PluginManifest) -> ContextPlugin:
    """Validate a context policy plugin and keep its factory lazy."""
    factory = _load_entry_factory(manifest, "context")
    return ContextPlugin(manifest=manifest, factory=factory)


def load_skill_plugin(manifest: PluginManifest) -> SkillPlugin:
    """校验单个技能插件：只检查清单与正文文件存在，不读取、不执行。"""
    where = f"{manifest.directory / 'plugin.json'} ('{manifest.name}')"
    entry = manifest.entry
    content_rel = entry.get("content", "SKILL.md")
    _expect(
        isinstance(content_rel, str) and content_rel.strip(),
        where,
        "skill 插件必须在 entry 里声明非空 'content'",
    )
    preload = entry.get("preload", False)
    _expect(isinstance(preload, bool), where, "'preload' 必须是布尔值")

    content_path = manifest.directory / content_rel
    _expect(content_path.is_file(), where, f"正文文件不存在: {content_path}")
    return SkillPlugin(manifest=manifest, content_path=content_path, preload=preload)


def load_session_plugin(manifest: PluginManifest) -> SessionPlugin:
    """校验单个会话存储插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "session")
    return SessionPlugin(manifest=manifest, factory=factory)


def load_memory_plugin(manifest: PluginManifest) -> MemoryPlugin:
    """校验单个长期记忆插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "memory")
    return MemoryPlugin(manifest=manifest, factory=factory)


def load_embedding_plugin(manifest: PluginManifest) -> EmbeddingPlugin:
    """校验单个嵌入提供方插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "embedding")
    return EmbeddingPlugin(manifest=manifest, factory=factory)


def load_listener_plugin(manifest: PluginManifest) -> ListenerPlugin:
    """校验单个 listener 插件并返回惰性工厂（不在此处实例化）。"""
    factory = _load_entry_factory(manifest, "listener")
    return ListenerPlugin(manifest=manifest, factory=factory)


def _resolve_arg(plugin_dir: Path, arg: str) -> str:
    """插件目录下真实存在的相对路径参数 -> 绝对路径；其余参数原样保留。"""
    path = Path(arg)
    if not path.is_absolute() and (plugin_dir / path).is_file():
        return str(plugin_dir / path)
    return arg


# ---------- 整目录加载：供单类使用与测试 ----------


def load_hook_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
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


def load_mcp_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[McpPluginSpec]:
    """加载插件目录里全部启用的 MCP 插件，产出可直接连接的规格。"""
    specs: list[McpPluginSpec] = []
    for manifest in discover_plugins(root):
        if manifest.type != "mcp":
            continue
        specs.append(load_mcp_plugin(manifest))
        logger.info("MCP 插件已发现: %s", manifest.name)
    return specs


def load_tool_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[tuple[PluginManifest, list[Tool]]]:
    """加载插件目录里全部启用的本地工具插件，返回 (清单, 工具列表)。"""
    plugins: list[tuple[PluginManifest, list[Tool]]] = []
    for manifest in discover_plugins(root):
        if manifest.type != "tool":
            continue
        plugins.append((manifest, load_tool_plugin(manifest)))
        logger.info("本地工具插件已加载: %s", manifest.name)
    return sorted(plugins, key=lambda pair: pair[0].name)


def load_model_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[ModelPlugin]:
    """加载插件目录里全部启用的模型插件，返回惰性规格（不实例化）。"""
    plugins: list[ModelPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "model":
            continue
        plugins.append(load_model_plugin(manifest))
        logger.info("模型插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_context_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[ContextPlugin]:
    """Load enabled context policy plugins without instantiating them."""
    plugins: list[ContextPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "context":
            continue
        plugins.append(load_context_plugin(manifest))
        logger.info("上下文策略插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_skill_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[SkillPlugin]:
    """加载插件目录里全部启用的技能插件（零副作用：不读正文、不执行）。"""
    plugins: list[SkillPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "skill":
            continue
        plugins.append(load_skill_plugin(manifest))
        logger.info("技能插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_session_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[SessionPlugin]:
    """加载插件目录里全部启用的会话存储插件（惰性，不实例化）。"""
    plugins: list[SessionPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "session":
            continue
        plugins.append(load_session_plugin(manifest))
        logger.info("会话存储插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_memory_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[MemoryPlugin]:
    """加载插件目录里全部启用的长期记忆插件（惰性，不实例化）。"""
    plugins: list[MemoryPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "memory":
            continue
        plugins.append(load_memory_plugin(manifest))
        logger.info("长期记忆插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_embedding_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[EmbeddingPlugin]:
    """加载插件目录里全部启用的嵌入提供方插件（惰性，不实例化）。"""
    plugins: list[EmbeddingPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "embedding":
            continue
        plugins.append(load_embedding_plugin(manifest))
        logger.info("嵌入提供方插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


def load_listener_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> list[ListenerPlugin]:
    """加载插件目录里全部启用的 listener 插件（惰性，不实例化）。"""
    plugins: list[ListenerPlugin] = []
    for manifest in discover_plugins(root):
        if manifest.type != "listener":
            continue
        plugins.append(load_listener_plugin(manifest))
        logger.info("listener 插件已发现: %s", manifest.name)
    return sorted(plugins, key=lambda plugin: plugin.manifest.name)


# ---------- kind 注册表与统一装配 ----------


@dataclass
class PluginAssembly:
    """一次装配的结果：按 kind 归好类的插件产物，交给对应网关/注册表。"""

    hooks: list[tuple[PluginManifest, LifecycleHooks]] = field(default_factory=list)
    mcp: list[McpPluginSpec] = field(default_factory=list)
    tools: list[tuple[PluginManifest, list[Tool]]] = field(default_factory=list)
    models: list[ModelPlugin] = field(default_factory=list)
    contexts: list[ContextPlugin] = field(default_factory=list)
    skills: list[SkillPlugin] = field(default_factory=list)
    sessions: list[SessionPlugin] = field(default_factory=list)
    memories: list[MemoryPlugin] = field(default_factory=list)
    embeddings: list[EmbeddingPlugin] = field(default_factory=list)
    listeners: list[ListenerPlugin] = field(default_factory=list)
    contributions: list[PluginContribution] = field(default_factory=list)


def _apply_hook(assembly: PluginAssembly, manifest: PluginManifest, hook: LifecycleHooks) -> None:
    assembly.hooks.append((manifest, hook))


def _apply_mcp(assembly: PluginAssembly, manifest: PluginManifest, spec: McpPluginSpec) -> None:
    assembly.mcp.append(spec)


def _apply_tool(assembly: PluginAssembly, manifest: PluginManifest, tools: list[Tool]) -> None:
    assembly.tools.append((manifest, tools))


def _apply_model(assembly: PluginAssembly, manifest: PluginManifest, plugin: ModelPlugin) -> None:
    assembly.models.append(plugin)


def _apply_context(
    assembly: PluginAssembly, manifest: PluginManifest, plugin: ContextPlugin
) -> None:
    assembly.contexts.append(plugin)


def _apply_skill(assembly: PluginAssembly, manifest: PluginManifest, plugin: SkillPlugin) -> None:
    assembly.skills.append(plugin)


def _apply_session(
    assembly: PluginAssembly, manifest: PluginManifest, plugin: SessionPlugin
) -> None:
    assembly.sessions.append(plugin)


def _apply_memory(assembly: PluginAssembly, manifest: PluginManifest, plugin: MemoryPlugin) -> None:
    assembly.memories.append(plugin)


def _apply_embedding(
    assembly: PluginAssembly, manifest: PluginManifest, plugin: EmbeddingPlugin
) -> None:
    assembly.embeddings.append(plugin)


def _apply_listener(
    assembly: PluginAssembly, manifest: PluginManifest, plugin: ListenerPlugin
) -> None:
    assembly.listeners.append(plugin)


# kind 注册表：受信任的核心代码在这里声明每类 contribution 的
# load/apply 契约。普通插件清单只能引用已注册的 kind。
register_kind(KindHandler("hook", load_hook_plugin, _apply_hook))
register_kind(KindHandler("mcp", load_mcp_plugin, _apply_mcp))
register_kind(KindHandler("tool", load_tool_plugin, _apply_tool))
register_kind(KindHandler("model", load_model_plugin, _apply_model))
register_kind(KindHandler("context", load_context_plugin, _apply_context))
register_kind(KindHandler("skill", load_skill_plugin, _apply_skill))
register_kind(KindHandler("session", load_session_plugin, _apply_session))
register_kind(KindHandler("memory", load_memory_plugin, _apply_memory))
register_kind(KindHandler("embedding", load_embedding_plugin, _apply_embedding))
register_kind(KindHandler("listener", load_listener_plugin, _apply_listener))

SUPPORTED_KINDS = registered_kinds()


def attach_listener_plugins(
    bus: EventBus, listeners: list[ListenerPlugin]
) -> list[Callable[[], None]]:
    """把 listener 插件订阅到事件总线，返回退订函数列表（热卸载预留）。"""
    tokens: list[Callable[[], None]] = []
    for plugin in listeners:
        for subscription in plugin.create():
            tokens.append(
                bus.subscribe(
                    subscription.event,
                    subscription.handler,
                    priority=subscription.priority,
                )
            )
        logger.info("listener 插件已接入事件总线: %s", plugin.manifest.name)
    return tokens


def assemble_plugins(
    root: str | Path | Sequence[str | Path] | None = None,
) -> PluginAssembly:
    """扫描目录并把全部 enabled contribution 按 kind 装配成 PluginAssembly。

    这是 Harness 启动器的统一装配入口；main.py 不感知具体插件类别。
    """
    assembly = PluginAssembly()
    for contribution in discover_contributions(root):
        manifest = contribution.manifest
        handler = _KIND_REGISTRY.get(contribution.kind)
        if handler is None:  # discover 已校验，这里是防御性兜底
            raise ValueError(f"插件类别 {contribution.kind} 尚未注册加载器（{manifest.name}）")
        payload = handler.load(manifest)
        handler.apply(assembly, manifest, payload)
        assembly.contributions.append(contribution)
    assembly.hooks.sort(key=lambda pair: (pair[0].priority, pair[0].name))
    return assembly
