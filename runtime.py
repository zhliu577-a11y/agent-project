# runtime.py - assemble and own the runnable Harness object graph
import logging
import os
from dataclasses import dataclass, replace
from typing import Any

from config import AppConfig
from core.context import ContextPolicy
from core.events import EventBus, jsonl_sink
from core.hooks import HookGateway
from core.model import ModelAdapter, ModelRouter
from core.prompt import build_system_prompt
from core.registry import ToolRegistry
from core.types import Message
from gateways.mcp_gateway import McpGateway, UsePlugin
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from gateways.model_gateway import ModelGateway
from gateways.session_gateway import SessionGateway
from gateways.skill_gateway import SkillGateway, UseSkill
from plugins.context import PluginContext
from plugins.lifecycle import PluginLifecycleManager
from plugins.loader import (
    PluginAssembly,
    PluginManifest,
    assemble_plugins,
    attach_listener_plugins,
)
from plugins.manager import PluginManager

logger = logging.getLogger(__name__)


class RuntimeStartupError(RuntimeError):
    """The runtime could not reach a usable state."""


@dataclass(frozen=True)
class PluginRuntimeStatus:
    """Actual runtime status for one built-in plugin package or external record."""

    name: str
    scope: str
    status: str
    enabled: bool
    contributions: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class McpRuntimeStatus:
    name: str
    status: str


@dataclass(frozen=True)
class ModelRuntimeStatus:
    name: str
    status: str
    active: bool


@dataclass(frozen=True)
class RuntimeSnapshot:
    """JSON-safe view of the runtime without exposing live gateway objects."""

    ready: bool
    plugins: tuple[PluginRuntimeStatus, ...]
    tools: tuple[str, ...]
    mcp: tuple[McpRuntimeStatus, ...]
    models: tuple[ModelRuntimeStatus, ...]


class HarnessRuntime:
    """Assemble plugins and own the gateways, tools, session, and model."""

    def __init__(
        self,
        config: AppConfig,
        *,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self.config = config
        self.plugin_manager = plugin_manager or PluginManager()
        self._external_names = {record.name for record in self.plugin_manager.list_installed()}
        self._plugin_states: dict[tuple[str, str], PluginRuntimeStatus] = {
            ("external", record.name): PluginRuntimeStatus(
                name=record.name,
                scope="external",
                status=record.runtime_status,
                enabled=record.enabled,
                contributions=(),
                error=record.last_error,
            )
            for record in self.plugin_manager.list_installed()
        }
        self._assembly = PluginAssembly()
        self._listener_tokens: list[Any] = []
        self._lifecycle = PluginLifecycleManager()
        self._started = False
        self._closed = False
        self._ready = False

        self.hooks = HookGateway()
        self.events = EventBus()
        self.tools = ToolRegistry()
        self.model: ModelAdapter | None = None
        self.models: ModelGateway | None = None
        self.context_policy: ContextPolicy | None = None
        self.session: SessionGateway | None = None
        self.memory: MemoryGateway | None = None
        self.skills: SkillGateway | None = None
        self.mcp_gateway: McpGateway | None = None
        self._use_plugin: UsePlugin | None = None
        self.system_prompt = ""
        self.history: list[Message] = []

    @property
    def ready(self) -> bool:
        return self._ready

    async def start(self) -> None:
        if self._started:
            raise RuntimeStartupError("runtime has already been started")
        if self._closed:
            raise RuntimeStartupError("runtime has already been closed")
        self._started = True
        try:
            self._prepare_external_statuses()
            self._assembly = self._assemble_plugins()
            self._setup_events()
            await self._create_model()
            self._create_context_policy()
            await self._create_session()
            self._create_memory()
            self._register_tools()
            self.mcp_gateway = McpGateway(self._assembly.mcp)
            self._use_plugin = UsePlugin(self.mcp_gateway, self.tools)
            self.tools.register(self._use_plugin)
            await self._preload_mcp_plugins()
            await self._lifecycle.start_all()
            self._build_system_prompt()
            self._ready = True
            logger.info(
                "Runtime 已就绪：插件包 %d 个，工具 %d 个，MCP 插件 %d 个",
                len(self._plugin_states),
                len(self.tools.list_schemas()),
                len(self._assembly.mcp),
            )
        except Exception:
            self._ready = False
            await self.close()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        self._close_listener_subscriptions()
        await self._lifecycle.stop_all()
        if self.models is not None:
            await self.models.close()
        if self.mcp_gateway is not None:
            await self.mcp_gateway.close()
        await self._close_external_tools()
        for key, state in list(self._plugin_states.items()):
            if key[0] == "external" and state.status == "active":
                self.plugin_manager.record_runtime_status(state.name, "stopped")
                self._plugin_states[key] = replace(state, status="stopped")
            elif key[0] == "builtin" and state.status == "active":
                self._plugin_states[key] = replace(state, status="stopped")

    def _close_listener_subscriptions(self) -> None:
        for unsubscribe in reversed(self._listener_tokens):
            try:
                unsubscribe()
            except Exception:
                logger.exception("listener unsubscribe failed")
        self._listener_tokens.clear()

    async def __aenter__(self) -> "HarnessRuntime":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _close_external_tools(self) -> None:
        """Close process-backed tools owned by this assembly."""
        for _manifest, tools in self._assembly.tools:
            for tool in tools:
                close = getattr(tool, "close", None)
                if not callable(close):
                    continue
                try:
                    await close()
                except Exception:
                    logger.exception("external tool cleanup failed: %s", tool.name)

    def snapshot(self) -> RuntimeSnapshot:
        tool_names = tuple(
            sorted(schema["function"]["name"] for schema in self.tools.list_schemas())
        )
        mcp = (
            tuple(
                McpRuntimeStatus(name=name, status=status)
                for name, status in sorted(self.mcp_gateway.status().items())
            )
            if self.mcp_gateway is not None
            else ()
        )
        return RuntimeSnapshot(
            ready=self._ready,
            plugins=tuple(
                state
                for _, state in sorted(
                    self._plugin_states.items(),
                    key=lambda item: (item[0][0], item[0][1]),
                )
            ),
            tools=tool_names,
            mcp=mcp,
            models=(
                tuple(
                    ModelRuntimeStatus(
                        name=name,
                        status=status,
                        active=name == self.models.active_model,
                    )
                    for name, status in sorted(self.models.status().items())
                )
                if self.models is not None
                else ()
            ),
        )

    def _prepare_external_statuses(self) -> None:
        for record in self.plugin_manager.list_installed():
            status = "idle" if record.enabled else "disabled"
            if record.runtime_status != status or record.last_error is not None:
                self.plugin_manager.record_runtime_status(record.name, status)
            self._plugin_states[("external", record.name)] = PluginRuntimeStatus(
                name=record.name,
                scope="external",
                status=status,
                enabled=record.enabled,
                contributions=(),
            )

    def _assemble_plugins(self) -> PluginAssembly:
        assembly = PluginAssembly()
        if self.plugin_manager.builtin_dir.is_dir():
            try:
                builtins = assemble_plugins(self.plugin_manager.builtin_dir, self.config)
            except Exception as exc:
                raise RuntimeStartupError(f"内置插件装配失败: {exc}") from exc
            self._record_builtin_states(builtins)
            _merge_assemblies(assembly, builtins)

        for record in self.plugin_manager.enabled_records():
            try:
                package = assemble_plugins(
                    self.plugin_manager.plugin_path(record.name),
                    self.config,
                )
                _merge_assemblies(assembly, package)
            except Exception as exc:
                logger.exception("外部插件 %s 装配失败", record.name)
                self._mark_external_error(record.name, exc)
                continue
            self._mark_external_active(record.name, package)
        return assembly

    def _record_builtin_states(self, assembly: PluginAssembly) -> None:
        grouped: dict[str, list[str]] = {}
        for contribution in assembly.contributions:
            grouped.setdefault(contribution.package_name, []).append(
                _contribution_label(contribution.kind, contribution.manifest.name)
            )
        for name, contributions in grouped.items():
            self._plugin_states[("builtin", name)] = PluginRuntimeStatus(
                name=name,
                scope="builtin",
                status="active",
                enabled=True,
                contributions=tuple(sorted(contributions)),
            )

    def _mark_external_active(self, name: str, assembly: PluginAssembly) -> None:
        contributions = tuple(
            sorted(
                _contribution_label(contribution.kind, contribution.manifest.name)
                for contribution in assembly.contributions
            )
        )
        record = self.plugin_manager.record_runtime_status(name, "active")
        self._plugin_states[("external", name)] = PluginRuntimeStatus(
            name=name,
            scope="external",
            status="active",
            enabled=record.enabled,
            contributions=contributions,
        )

    def _mark_external_error(self, name: str, exc: Exception) -> None:
        message = str(exc)
        previous = self._plugin_states.get(("external", name))
        record = self.plugin_manager.record_runtime_status(
            name,
            "error",
            last_error=message,
        )
        self._plugin_states[("external", name)] = PluginRuntimeStatus(
            name=name,
            scope="external",
            status="error",
            enabled=record.enabled,
            contributions=previous.contributions if previous is not None else (),
            error=message,
        )

    def _mark_manifest_error(self, manifest: PluginManifest, exc: Exception) -> None:
        package_name = manifest.package_name or manifest.name
        if package_name in self._external_names:
            self._mark_external_error(package_name, exc)
            return
        key = ("builtin", package_name)
        state = self._plugin_states.get(key)
        if state is not None:
            self._plugin_states[key] = replace(state, status="error", error=str(exc))

    def _setup_events(self) -> None:
        event_log = os.getenv("EVENT_LOG")
        if event_log:
            self.events.subscribe("*", jsonl_sink(event_log))
            logger.info("事件总线日志已启用: %s", event_log)
        for manifest, hook in self._assembly.hooks:
            try:
                self.hooks.add(
                    hook,
                    priority=manifest.priority,
                    name=manifest.name,
                    spec=manifest.hook,
                )
                self._track_plugin(manifest, hook)
            except Exception as exc:
                logger.exception("hook plugin %s setup failed", manifest.name)
                self._mark_manifest_error(manifest, exc)
        self.hooks.attach(self.events)
        for listener in self._assembly.listeners:
            try:
                self._listener_tokens.extend(attach_listener_plugins(self.events, [listener]))
            except Exception as exc:
                logger.exception("listener 插件 %s 接入失败", listener.manifest.name)
                self._mark_manifest_error(listener.manifest, exc)

    async def _create_model(self) -> None:
        available = [candidate.manifest.name for candidate in self._assembly.models]
        if self.config.model not in available:
            raise RuntimeStartupError(f"未知的模型插件: {self.config.model}，可选: {available}")

        router: ModelRouter | None = None
        if self.config.model_router is not None:
            router_plugin = next(
                (
                    candidate
                    for candidate in self._assembly.model_routers
                    if candidate.manifest.name == self.config.model_router
                ),
                None,
            )
            if router_plugin is None:
                raise RuntimeStartupError(
                    f"未知的模型路由插件: {self.config.model_router}，可选: "
                    f"{[candidate.manifest.name for candidate in self._assembly.model_routers]}"
                )
            try:
                router = router_plugin.create()
                self._track_plugin(router_plugin.manifest, router)
            except Exception as exc:
                self._mark_manifest_error(router_plugin.manifest, exc)
                raise RuntimeStartupError(
                    f"模型路由插件 {self.config.model_router} 初始化失败: {exc}"
                ) from exc

        try:
            gateway = ModelGateway(
                self._assembly.models,
                default_model=self.config.model,
                fallback=self.config.model_fallback,
                routes=dict(self.config.model_routes),
                router=router,
            )
            self.models = gateway
            self.model = gateway
            await gateway.prewarm(self.config.model)
            for name in self.config.model_keep_warm:
                await gateway.prewarm(name)
        except Exception as exc:
            raise RuntimeStartupError(f"模型网关初始化失败: {exc}") from exc

    def _create_context_policy(self) -> None:
        plugin = next(
            (
                candidate
                for candidate in self._assembly.contexts
                if candidate.manifest.name == self.config.context_strategy
            ),
            None,
        )
        if plugin is None:
            raise RuntimeStartupError(
                f"未知的上下文策略插件: {self.config.context_strategy}，可选: "
                f"{[candidate.manifest.name for candidate in self._assembly.contexts]}"
            )
        try:
            self.context_policy = plugin.create()
            self._track_plugin(plugin.manifest, self.context_policy)
        except Exception as exc:
            self._mark_manifest_error(plugin.manifest, exc)
            raise RuntimeStartupError(
                f"上下文策略插件 {self.config.context_strategy} 初始化失败: {exc}"
            ) from exc

    async def _create_session(self) -> None:
        plugin = next(
            (
                candidate
                for candidate in self._assembly.sessions
                if candidate.manifest.name == self.config.session_store
            ),
            None,
        )
        if plugin is None:
            raise RuntimeStartupError(
                f"未知的会话存储插件: {self.config.session_store}，可选: "
                f"{[candidate.manifest.name for candidate in self._assembly.sessions]}"
            )
        try:
            store = plugin.create()
            self._track_plugin(plugin.manifest, store)
        except Exception as exc:
            self._mark_manifest_error(plugin.manifest, exc)
            raise RuntimeStartupError(
                f"会话存储插件 {self.config.session_store} 初始化失败: {exc}"
            ) from exc
        self.session = SessionGateway(store, session_id=self.config.session_id)
        try:
            self.history = await self.session.load_history()
        except (OSError, ValueError) as exc:
            logger.warning("会话历史读取失败，将开启新会话: %s", exc)
            self.history = []

    def _create_memory(self) -> None:
        plugin = next(
            (
                candidate
                for candidate in self._assembly.memories
                if candidate.manifest.name == self.config.memory_store
            ),
            None,
        )
        if plugin is None:
            raise RuntimeStartupError(
                f"未知的长期记忆插件: {self.config.memory_store}，可选: "
                f"{[candidate.manifest.name for candidate in self._assembly.memories]}"
            )
        try:
            store = plugin.create()
            self._track_plugin(plugin.manifest, store)
        except Exception as exc:
            self._mark_manifest_error(plugin.manifest, exc)
            raise RuntimeStartupError(
                f"长期记忆插件 {self.config.memory_store} 初始化失败: {exc}"
            ) from exc
        self.memory = MemoryGateway(store, events=self.events)

    def _register_tools(self) -> None:
        self.tools = ToolRegistry()
        for _, tools in self._assembly.tools:
            for tool in tools:
                self.tools.register(tool)
        for manifest, tools in self._assembly.tools:
            for tool in tools:
                self._track_plugin(manifest, tool)

        self.skills = SkillGateway(self._assembly.skills)
        self.tools.register(UseSkill(self.skills))
        if self.memory is None:
            raise RuntimeStartupError("长期记忆网关未初始化")
        self.tools.register(RememberTool(self.memory))
        self.tools.register(RecallTool(self.memory))
        self.tools.register(ForgetTool(self.memory))
        self.tools.register(UpdateNoteTool(self.memory))

    async def _preload_mcp_plugins(self) -> None:
        if self.mcp_gateway is None or self._use_plugin is None:
            raise RuntimeStartupError("MCP 网关未初始化")

        unknown = [
            name for name in self.config.mcp_preload if self.mcp_gateway.plugin_spec(name) is None
        ]
        if unknown:
            raise RuntimeStartupError(
                f"未知的 MCP 预加载插件: {unknown}，可用: {self.mcp_gateway.available()}"
            )

        for name in self.config.mcp_preload:
            ok, message = await self._use_plugin.mount(name)
            if ok:
                logger.info("MCP 插件已预加载: %s", name)
            else:
                logger.warning(
                    "MCP 插件预加载失败，后续仍可通过 use_plugin 重试: %s (%s)",
                    name,
                    message,
                )

    def _build_system_prompt(self) -> None:
        if self.skills is None:
            raise RuntimeStartupError("技能网关未初始化")
        preloads: list[tuple[str, str]] = []
        for skill in self._assembly.skills:
            if not skill.preload:
                continue
            try:
                preloads.append((skill.manifest.name, self.skills.get(skill.manifest.name)))
            except ValueError as exc:
                logger.warning("预载技能 %s 读取失败，已跳过: %s", skill.manifest.name, exc)
        self.system_prompt = build_system_prompt(
            mcp=[(spec.manifest.name, spec.manifest.description) for spec in self._assembly.mcp],
            skills=self.skills.catalog(),
            preloads=preloads,
        )

    def _track_plugin(self, manifest: PluginManifest, instance: object) -> None:
        context = PluginContext.create(
            name=manifest.name,
            kind=manifest.type,
            directory=manifest.directory,
            config=self.config.plugin_config(manifest.type, manifest.name),
            package_name=manifest.package_name,
            contribution_id=manifest.contribution_id,
            protocol_version=manifest.protocol_version,
            contract=manifest.contract,
        )
        self._lifecycle.add(context, instance)


def _merge_assemblies(target: PluginAssembly, source: PluginAssembly) -> None:
    seen = {
        (contribution.kind, contribution.manifest.name) for contribution in target.contributions
    }
    for contribution in source.contributions:
        key = (contribution.kind, contribution.manifest.name)
        if key in seen:
            manifest = contribution.manifest
            raise ValueError(f"插件重名（{manifest.type}/{manifest.name}）: {manifest.directory}")
        seen.add(key)

    target.hooks.extend(source.hooks)
    target.mcp.extend(source.mcp)
    target.tools.extend(source.tools)
    target.models.extend(source.models)
    target.model_routers.extend(source.model_routers)
    target.contexts.extend(source.contexts)
    target.skills.extend(source.skills)
    target.sessions.extend(source.sessions)
    target.memories.extend(source.memories)
    target.embeddings.extend(source.embeddings)
    target.listeners.extend(source.listeners)
    target.contributions.extend(source.contributions)
    target.hooks.sort(key=lambda pair: (pair[0].priority, pair[0].name))


def _contribution_label(kind: str, name: str) -> str:
    return f"{kind}:{name}"
