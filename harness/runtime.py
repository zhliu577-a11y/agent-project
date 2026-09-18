# runtime.py - assemble and own the runnable Harness object graph
import inspect
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Any

from config import AppConfig
from core.compaction import CompactionPolicy
from core.context import ContextPolicy
from core.events import Event, EventGateway, EventIdentity, jsonl_sink
from core.hooks import HookGateway
from core.memory import MemoryRetriever
from core.memory_extraction import MemoryExtractor
from core.model import ModelAdapter, ModelRouter
from core.prompt import build_system_prompt
from core.registry import ToolRegistry
from core.retry import RetryExecutor, RetryingTool
from core.types import Message
from gateways.context_gateway import ContextGateway
from gateways.mcp_gateway import McpGateway, UsePlugin
from gateways.memory_extraction_gateway import MemoryExtractionGateway
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from gateways.model_gateway import ModelGateway
from gateways.session_gateway import SessionGateway
from gateways.skill_gateway import (
    ApprovalCallback,
    SkillGateway,
    SkillPermissionPolicy,
    SkillUsageStats,
    UseSkill,
)
from plugins.context import PluginContext
from plugins.lifecycle import PluginLifecycleManager
from plugins.loader import (
    PluginAssembly,
    PluginManifest,
    assemble_plugins,
    attach_listener_plugins,
)
from plugins.manager import PluginManager
from plugins.services import (
    RuntimeServices,
    ServiceRef,
    ServiceResolutionError,
    candidate_service_names,
    resolve_dependency_order,
)

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
    preload: bool


@dataclass(frozen=True)
class ModelRuntimeStatus:
    name: str
    status: str
    active: bool


@dataclass(frozen=True)
class SkillRuntimeStatus:
    name: str
    status: str
    preload: bool
    loaded: bool
    bytes: int
    access: str = "allow"
    priority: int = 0
    listing: str = "full"
    usage: SkillUsageStats = field(default_factory=SkillUsageStats)
    error: str | None = None


@dataclass(frozen=True)
class RuntimeSnapshot:
    """JSON-safe view of the runtime without exposing live gateway objects."""

    ready: bool
    plugins: tuple[PluginRuntimeStatus, ...]
    tools: tuple[str, ...]
    mcp: tuple[McpRuntimeStatus, ...]
    models: tuple[ModelRuntimeStatus, ...]
    skills: tuple[SkillRuntimeStatus, ...]


class HarnessRuntime:
    """Assemble plugins and own the gateways, tools, session, and model."""

    def __init__(
        self,
        config: AppConfig,
        *,
        plugin_manager: PluginManager | None = None,
        skill_approver: ApprovalCallback | None = None,
    ) -> None:
        self.config = config
        self.plugin_manager = plugin_manager or PluginManager()
        self._skill_approver = skill_approver
        self.retry_executor: RetryExecutor | None = None
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
        self.services = RuntimeServices()
        self._listener_tokens: list[Any] = []
        self._lifecycle = PluginLifecycleManager()
        self._started = False
        self._closed = False
        self._ready = False

        self.hooks = HookGateway()
        self.events: EventGateway | None = None
        self.tools = ToolRegistry()
        self.model: ModelAdapter | None = None
        self.models: ModelGateway | None = None
        self.context_policy: ContextPolicy | None = None
        self.context_gateway: ContextPolicy | None = None
        self.compaction_policy: CompactionPolicy | None = None
        self.session_store: object | None = None
        self.memory_store: object | None = None
        self.memory_index: object | None = None
        self.memory_retriever: MemoryRetriever | None = None
        self.memory_policy: object | None = None
        self.memory_extractor: MemoryExtractor | None = None
        self.session: SessionGateway | None = None
        self.session_checkpoint: dict[str, Any] | None = None
        self.memory: MemoryGateway | None = None
        self.memory_extraction: MemoryExtractionGateway | None = None
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
            self._create_event_transport()
            self._setup_events()
            self._create_retry_executor()
            await self._create_model()
            await self._create_runtime_services()
            await self._create_session_gateway()
            self._create_memory_gateway()
            await self._initialize_memory_index()
            self._create_context_gateway()
            self._create_memory_extraction_gateway()
            self._register_tools()
            self.mcp_gateway = McpGateway(
                self._assembly.mcp,
                retry=self.retry_executor,
            )
            self._use_plugin = UsePlugin(self.mcp_gateway, self.tools)
            self.tools.register(self._use_plugin)
            await self._preload_mcp_plugins()
            await self._lifecycle.start_all()
            await self._build_system_prompt()
            await self._require_events().flush()
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
        if self.events is not None:
            try:
                await self.events.flush()
            except Exception:
                logger.exception("event bus flush failed during shutdown")
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
        mcp = ()
        if self.mcp_gateway is not None:
            mcp = tuple(
                McpRuntimeStatus(
                    name=name,
                    status=status,
                    preload=name in self.config.mcp_preload,
                )
                for name, status in sorted(self.mcp_gateway.status().items())
            )
        skill_entries = (
            {entry.name: entry for entry in self.skills.entries()}
            if self.skills is not None
            else {}
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
            skills=(
                tuple(
                    SkillRuntimeStatus(
                        name=name,
                        status=(
                            "error"
                            if self.skills.error(name) is not None
                            else "loaded"
                            if self.skills.is_loaded(name)
                            else "ready"
                        ),
                        preload=name in self.config.skill_preload,
                        loaded=self.skills.is_loaded(name),
                        bytes=self.skills.loaded_bytes(name),
                        access=skill_entries[name].access,
                        priority=skill_entries[name].priority,
                        listing=skill_entries[name].listing,
                        usage=self.skills.usage(name),
                        error=self.skills.error(name),
                    )
                    for name in self.skills.available()
                )
                if self.skills is not None
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
                builtins = assemble_plugins(
                    self.plugin_manager.builtin_dir,
                    self.config,
                    services=self.services,
                )
            except Exception as exc:
                raise RuntimeStartupError(f"内置插件装配失败: {exc}") from exc
            self._record_builtin_states(builtins)
            _merge_assemblies(assembly, builtins)

        for record in self.plugin_manager.enabled_records():
            try:
                package = assemble_plugins(
                    self.plugin_manager.plugin_path(record.name),
                    self.config,
                    services=self.services,
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
        events = self._require_events()
        event_log = os.getenv("EVENT_LOG")
        if event_log:
            events.subscriber(EventIdentity.host("event-log")).subscribe(
                "*",
                jsonl_sink(event_log),
            )
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
        self.hooks.attach(events)
        for listener in self._assembly.listeners:
            try:
                self._listener_tokens.extend(attach_listener_plugins(events, [listener]))
            except Exception as exc:
                logger.exception("listener 插件 %s 接入失败", listener.manifest.name)
                self._mark_manifest_error(listener.manifest, exc)

    def _create_event_transport(self) -> None:
        available = [candidate.manifest.name for candidate in self._assembly.event_transports]
        plugin = next(
            (
                candidate
                for candidate in self._assembly.event_transports
                if candidate.manifest.name == self.config.event_transport
            ),
            None,
        )
        if plugin is None:
            raise RuntimeStartupError(
                "unknown event transport plugin: "
                f"{self.config.event_transport}; available: {available}"
            )
        try:
            transport = plugin.create()
            self.events = EventGateway(
                transport=transport,
                handler_timeout=self.config.event_handler_timeout,
            )
            self._track_plugin(plugin.manifest, transport)
        except Exception as exc:
            self._mark_manifest_error(plugin.manifest, exc)
            raise RuntimeStartupError(
                f"event transport plugin {self.config.event_transport} initialization failed: {exc}"
            ) from exc

    def _require_events(self) -> EventGateway:
        if self.events is None:
            raise RuntimeStartupError("event gateway is not initialized")
        return self.events

    def _create_retry_executor(self) -> None:
        routes = dict(self.config.retry_operation_policies)
        if self.config.retry_policy is None and not routes:
            self.retry_executor = None
            return
        required = {*routes.values()}
        if self.config.retry_policy is not None:
            required.add(self.config.retry_policy)
        available = {plugin.manifest.name: plugin for plugin in self._assembly.retry_policies}
        missing = sorted(required - set(available))
        if missing:
            raise RuntimeStartupError(
                f"unknown retry policy plugins: {missing}; available: {sorted(available)}"
            )

        policies: dict[str, object] = {}
        for name in sorted(required):
            plugin = available[name]
            try:
                policies[name] = plugin.create()
                self._track_plugin(plugin.manifest, policies[name])
            except Exception as exc:
                self._mark_manifest_error(plugin.manifest, exc)
                raise RuntimeStartupError(
                    f"retry policy plugin {name} initialization failed: {exc}"
                ) from exc

        try:
            self.retry_executor = RetryExecutor(
                policies,  # type: ignore[arg-type]
                default_policy=self.config.retry_policy,
                routes=routes,
                max_attempts=self.config.retry_max_attempts,
                max_delay=self.config.retry_max_delay,
                total_timeout=self.config.retry_total_timeout,
                events=self._require_events().publisher(EventIdentity.host("retry-executor")),
            )
        except Exception as exc:
            raise RuntimeStartupError(f"retry executor initialization failed: {exc}") from exc

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
                retry=self.retry_executor,
            )
            self.models = gateway
            self.model = gateway
            await gateway.prewarm(self.config.model)
            for name in self.config.model_keep_warm:
                await gateway.prewarm(name)
        except Exception as exc:
            raise RuntimeStartupError(f"模型网关初始化失败: {exc}") from exc

    def _service_catalog(self) -> dict[tuple[str, str], PluginManifest]:
        catalog: dict[tuple[str, str], PluginManifest] = {}
        for collection in (
            self._assembly.contexts,
            self._assembly.compactions,
            self._assembly.sessions,
            self._assembly.memories,
            self._assembly.memory_indexes,
            self._assembly.memory_retrievers,
            self._assembly.memory_policies,
            self._assembly.memory_extractors,
            self._assembly.embeddings,
        ):
            for plugin in collection:
                catalog[(plugin.manifest.type, plugin.manifest.name)] = plugin.manifest
        return catalog

    def _selected_services(self) -> dict[str, str]:
        selected = {
            "context": self.config.context_strategy,
            "session": self.config.session_store,
            "memory": self.config.memory_store,
            "embedding": self.config.embedding_provider,
        }
        if self.config.session_compaction is not None:
            selected["compaction"] = self.config.session_compaction
        if self.config.memory_index is not None:
            selected["memory-index"] = self.config.memory_index
        if self.config.memory_retriever is not None:
            selected["memory-retriever"] = self.config.memory_retriever
        if self.config.memory_policy is not None:
            selected["memory-policy"] = self.config.memory_policy
        if self.config.memory_extractor is not None:
            selected["memory-extractor"] = self.config.memory_extractor
        return selected

    def _service_roots(self) -> list[ServiceRef]:
        roots = [
            ServiceRef("context", self.config.context_strategy),
            ServiceRef("session", self.config.session_store),
            ServiceRef("memory", self.config.memory_store),
        ]
        if self.config.session_compaction is not None:
            roots.append(ServiceRef("compaction", self.config.session_compaction))
        if self.config.memory_index is not None:
            roots.append(ServiceRef("memory-index", self.config.memory_index))
        if self.config.memory_retriever is not None:
            roots.append(ServiceRef("memory-retriever", self.config.memory_retriever))
        if self.config.memory_policy is not None:
            roots.append(ServiceRef("memory-policy", self.config.memory_policy))
        if self.config.memory_extractor is not None:
            roots.append(ServiceRef("memory-extractor", self.config.memory_extractor))
        return roots

    def _service_plugin(self, ref: ServiceRef):
        collections = {
            "context": self._assembly.contexts,
            "compaction": self._assembly.compactions,
            "session": self._assembly.sessions,
            "memory": self._assembly.memories,
            "memory-index": self._assembly.memory_indexes,
            "memory-retriever": self._assembly.memory_retrievers,
            "memory-policy": self._assembly.memory_policies,
            "memory-extractor": self._assembly.memory_extractors,
            "embedding": self._assembly.embeddings,
        }
        plugin = next(
            (
                candidate
                for candidate in collections.get(ref.kind, ())
                if candidate.manifest.name == ref.name
            ),
            None,
        )
        if plugin is None:
            available = [candidate.manifest.name for candidate in collections.get(ref.kind, ())]
            raise RuntimeStartupError(
                f"unknown {ref.kind} plugin: {ref.name}; available: {available}"
            )
        return plugin

    async def _create_runtime_services(self) -> None:
        selected = self._selected_services()
        for kind, name in selected.items():
            self.services.select(kind, name)

        try:
            order = resolve_dependency_order(
                self._service_roots(),
                catalog=self._service_catalog(),
                selected=selected,
            )
        except ServiceResolutionError as exc:
            raise RuntimeStartupError(f"plugin dependency graph failed: {exc}") from exc

        created: list[tuple[PluginManifest, object]] = []
        current_plugin = None
        try:
            for ref in order:
                current_plugin = self._service_plugin(ref)
                instance = current_plugin.create()
                self.services.register(
                    ref.kind,
                    ref.name,
                    instance,
                    aliases=candidate_service_names(current_plugin.manifest),
                )
                created.append((current_plugin.manifest, instance))
        except Exception as exc:
            await self._rollback_service_instances(created)
            if current_plugin is not None:
                self._mark_manifest_error(current_plugin.manifest, exc)
            raise RuntimeStartupError(f"runtime service assembly failed: {exc}") from exc

        for manifest, instance in created:
            self._track_plugin(manifest, instance)

        for ref in order:
            instance = self.services.require(ref.kind, ref.name)
            if ref.kind == "context":
                self.context_policy = instance  # type: ignore[assignment]
            elif ref.kind == "compaction":
                self.compaction_policy = instance  # type: ignore[assignment]
            elif ref.kind == "session":
                self.session_store = instance
            elif ref.kind == "memory":
                self.memory_store = instance
            elif ref.kind == "memory-index":
                self.memory_index = instance
            elif ref.kind == "memory-retriever":
                self.memory_retriever = instance  # type: ignore[assignment]
            elif ref.kind == "memory-policy":
                self.memory_policy = instance
            elif ref.kind == "memory-extractor":
                self.memory_extractor = instance  # type: ignore[assignment]

    async def _rollback_service_instances(
        self,
        created: list[tuple[PluginManifest, object]],
    ) -> None:
        for _manifest, instance in reversed(created):
            stop = getattr(instance, "stop", None)
            close = getattr(instance, "close", None)
            cleanup = stop if callable(stop) else close if callable(close) else None
            if cleanup is None:
                continue
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("service rollback failed")

    async def _create_session_gateway(self) -> None:
        if self.session_store is None:
            raise RuntimeStartupError("session store service is not initialized")
        self.session = SessionGateway(
            self.session_store,  # type: ignore[arg-type]
            session_id=self.config.session_id,
            compaction=self.compaction_policy,
            max_tokens=self.config.context_max_tokens,
            events=self._require_events().publisher(EventIdentity.host("session-gateway")),
        )
        try:
            snapshot = await self.session.load_snapshot()
            self.history = list(snapshot.messages)
            self.session_checkpoint = snapshot.checkpoint
        except (OSError, ValueError) as exc:
            logger.warning("session history load failed; starting a new session: %s", exc)
            self.history = []
            self.session_checkpoint = None

    def _create_memory_gateway(self) -> None:
        if self.memory_store is None:
            raise RuntimeStartupError("memory store service is not initialized")
        self.memory = MemoryGateway(
            self.memory_store,  # type: ignore[arg-type]
            events=self._require_events().publisher(EventIdentity.host("memory-gateway")),
            retriever=self.memory_retriever,
            index=self.memory_index,  # type: ignore[arg-type]
            policy=self.memory_policy,  # type: ignore[arg-type]
            default_scope=self.config.memory_scope,
            default_owner_id=self.config.memory_owner_id,
            default_agent_id=self.config.memory_agent_id,
            default_tenant_id=self.config.memory_tenant_id,
        )

    def _create_context_gateway(self) -> None:
        if self.context_policy is None:
            raise RuntimeStartupError("context policy service is not initialized")
        self.context_gateway = ContextGateway(
            self.context_policy,
            memory=self.memory,
            default_scope=self.config.memory_scope,
            default_owner_id=self.config.memory_owner_id,
            default_agent_id=self.config.memory_agent_id,
            default_tenant_id=self.config.memory_tenant_id,
        )

    def _create_memory_extraction_gateway(self) -> None:
        if self.memory_extractor is None or self.memory is None:
            self.memory_extraction = None
            return
        self.memory_extraction = MemoryExtractionGateway(
            self.memory_extractor,
            self.memory,
            events=self._require_events().publisher(
                EventIdentity.host("memory-extraction-gateway")
            ),
        )

    async def _initialize_memory_index(self) -> None:
        if self.memory_store is None or self.memory is None:
            return
        try:
            records = await self.memory_store.list_notes()  # type: ignore[attr-defined]
            await self.memory.retriever.rebuild(records)
            await self.memory.maintain()
        except Exception as exc:
            raise RuntimeStartupError(f"memory retriever initialization failed: {exc}") from exc

    def _register_tools(self) -> None:
        self.tools = ToolRegistry()
        for manifest, tools in self._assembly.tools:
            for tool in tools:
                registered = tool
                if self.retry_executor is not None and manifest.retry_safe:
                    registered = RetryingTool(
                        tool,
                        self.retry_executor,
                        component=f"{manifest.type}:{manifest.name}",
                    )
                self.tools.register(registered)
        for manifest, tools in self._assembly.tools:
            for tool in tools:
                self._track_plugin(manifest, tool)

        self.skills = SkillGateway(
            self._assembly.skills,
            max_content_bytes=self.config.skill_max_content_bytes,
            max_resource_bytes=self.config.skill_max_resource_bytes,
            max_resource_total_bytes=self.config.skill_max_resource_total_bytes,
            max_listing_bytes=self.config.skill_max_listing_bytes,
            policy=SkillPermissionPolicy(
                global_rules=self.config.skill_permissions,
                agent_rules=self.config.skill_agent_permissions,
                agent=self.config.skill_agent,
            ),
            name_only=self.config.skill_name_only,
        )
        self.tools.register(
            UseSkill(
                self.skills,
                events=self._require_events().publisher(EventIdentity.host("skill-gateway")),
                approver=self._skill_approver,
            )
        )
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

    async def _build_system_prompt(self) -> None:
        if self.skills is None:
            raise RuntimeStartupError("技能网关未初始化")

        available = set(self.skills.available())
        unknown = [name for name in self.config.skill_preload if name not in available]
        if unknown:
            raise RuntimeStartupError(
                f"未知的 Skill 预加载插件: {unknown}，可选: {self.skills.available()}"
            )

        selected = set(self.config.skill_preload)
        for skill in self._assembly.skills:
            if skill.preload and skill.manifest.name not in selected:
                logger.warning(
                    "技能 %s 的 entry.preload 已忽略；请在 config/skill.json 的 preload 中显式选择",
                    skill.manifest.name,
                )

        preloads: list[tuple[str, str]] = []
        total_bytes = 0
        for name in self.config.skill_preload:
            decision = self.skills.permission(name)
            if decision.access != "allow":
                raise RuntimeStartupError(f"Skill 预加载被权限策略拒绝: {name} ({decision.access})")
            try:
                content = self.skills.get(name)
            except ValueError as exc:
                raise RuntimeStartupError(f"Skill 预加载失败 {name}: {exc}") from exc
            size = len(content.encode("utf-8"))
            total_bytes += size
            if total_bytes > self.config.skill_max_preload_bytes:
                raise RuntimeStartupError(
                    "Skill 预加载总大小超过预算: "
                    f"{total_bytes} > {self.config.skill_max_preload_bytes} bytes"
                )
            preloads.append((name, content))
            await (
                self._require_events()
                .publisher(EventIdentity.host("runtime"))
                .publish(
                    Event(
                        "skill.preloaded",
                        {
                            "name": name,
                            "bytes": size,
                        },
                    )
                )
            )

        try:
            skill_listing = self.skills.listing()
        except ValueError as exc:
            raise RuntimeStartupError(f"Skill listing budget failed: {exc}") from exc

        self.system_prompt = build_system_prompt(
            mcp=[(spec.manifest.name, spec.manifest.description) for spec in self._assembly.mcp],
            skills=skill_listing,
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
            requires=manifest.requires,
            services=self.services,
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
    target.compactions.extend(source.compactions)
    target.skills.extend(source.skills)
    target.sessions.extend(source.sessions)
    target.memories.extend(source.memories)
    target.memory_indexes.extend(source.memory_indexes)
    target.memory_retrievers.extend(source.memory_retrievers)
    target.memory_policies.extend(source.memory_policies)
    target.memory_extractors.extend(source.memory_extractors)
    target.embeddings.extend(source.embeddings)
    target.listeners.extend(source.listeners)
    target.event_transports.extend(source.event_transports)
    target.retry_policies.extend(source.retry_policies)
    target.contributions.extend(source.contributions)
    target.hooks.sort(key=lambda pair: (pair[0].priority, pair[0].name))


def _contribution_label(kind: str, name: str) -> str:
    return f"{kind}:{name}"
