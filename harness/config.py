# config.py - application and plugin configuration loader
#
# Layout:
#   config/config.json   shared runtime selections and defaults
#   config/model.json    model selection
#   config/session.json  session selection
#   config/memory.json   memory selection
#   config/embedding.json
#   config/event.json
#   config/event_transport.json
#   config/retry.json
#   config/context.json
#   config/mcp.json
#   config/skill.json
#   config/plugins/<kind>/<name>.json
#
# Merge order: environment variables > individual config files >
# config/config.json > built-in defaults.
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.json"

_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_SKILL_MAX_CONTENT_BYTES = 256 * 1024
DEFAULT_SKILL_MAX_PRELOAD_BYTES = 512 * 1024
DEFAULT_SKILL_MAX_RESOURCES = 32
DEFAULT_SKILL_MAX_RESOURCE_BYTES = 256 * 1024
DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES = 1024 * 1024
DEFAULT_SKILL_MAX_DESCRIPTION_BYTES = 512
DEFAULT_SKILL_MAX_RESOURCE_DESCRIPTION_BYTES = 256
DEFAULT_SKILL_MAX_LISTING_BYTES = 8192
_SKILL_PERMISSIONS = frozenset({"allow", "ask", "deny"})
_SKILL_AGENT_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SECTION_FILES = {
    "event": "event.json",
    "event_transport": "event_transport.json",
    "retry": "retry.json",
    "model": "model.json",
    "session": "session.json",
    "memory": "memory.json",
    "embedding": "embedding.json",
    "context": "context.json",
    "mcp": "mcp.json",
    "skill": "skill.json",
}


@dataclass(frozen=True)
class AppConfig:
    event_transport: str = "in-process"
    event_handler_timeout: float | None = None
    retry_policy: str | None = None
    retry_operation_policies: tuple[tuple[str, str], ...] = ()
    retry_max_attempts: int = 3
    retry_max_delay: float = 2.0
    retry_total_timeout: float = 30.0
    model: str = "deepseek"
    model_fallback: tuple[str, ...] = ()
    model_routes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    model_keep_warm: tuple[str, ...] = ()
    model_router: str | None = None
    session_store: str = "jsonl"
    session_id: str = "default"
    session_compaction: str | None = None
    memory_store: str = "sqlite"
    memory_index: str | None = None
    memory_retriever: str | None = "store-native"
    memory_policy: str | None = None
    memory_extractor: str | None = "explicit"
    memory_scope: str = "user"
    memory_owner_id: str = ""
    memory_agent_id: str = ""
    memory_tenant_id: str = ""
    embedding_provider: str = "debug"
    context_max_tokens: int = 20000
    context_strategy: str = "tail-window"
    mcp_preload: tuple[str, ...] = ()
    skill_preload: tuple[str, ...] = ()
    skill_max_content_bytes: int = DEFAULT_SKILL_MAX_CONTENT_BYTES
    skill_max_preload_bytes: int = DEFAULT_SKILL_MAX_PRELOAD_BYTES
    skill_max_resources: int = DEFAULT_SKILL_MAX_RESOURCES
    skill_max_resource_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_BYTES
    skill_max_resource_total_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES
    skill_max_description_bytes: int = DEFAULT_SKILL_MAX_DESCRIPTION_BYTES
    skill_max_resource_description_bytes: int = DEFAULT_SKILL_MAX_RESOURCE_DESCRIPTION_BYTES
    skill_max_listing_bytes: int = DEFAULT_SKILL_MAX_LISTING_BYTES
    skill_agent: str = "default"
    skill_permissions: tuple[tuple[str, str], ...] = ()
    skill_agent_permissions: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    skill_name_only: tuple[str, ...] = ()
    config_dir: Path | None = None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        """Load the merged configuration and fail fast on invalid values."""
        source = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        config_path = source / "config.json" if source.is_dir() else source
        raw = cls._read_json(config_path) if config_path.exists() else {}
        raw = _merge_section_files(config_path.parent, raw)

        session = cls._nested(raw, "session")
        memory = cls._nested(raw, "memory")
        embedding = cls._nested(raw, "embedding")
        context = cls._nested(raw, "context")
        mcp = cls._nested(raw, "mcp")
        skill = cls._nested(raw, "skill")
        event = cls._nested(raw, "event")
        event_transport = cls._nested(raw, "event_transport")
        retry = cls._nested(raw, "retry")
        model_options = cls._nested(raw, "_model_options")
        memory_index = _load_optional_name(
            memory.get("index"),
            os.getenv("MEMORY_INDEX"),
            "memory.index",
        )
        retriever_configured = "retriever" in memory or "MEMORY_RETRIEVER" in os.environ
        memory_retriever = _load_optional_name(
            memory.get("retriever"),
            os.getenv("MEMORY_RETRIEVER"),
            "memory.retriever",
        )
        if not retriever_configured:
            # Old installations may still select a memory-index plugin.
            # Otherwise the default is an explicit store-native retriever.
            memory_retriever = None if memory_index is not None else "store-native"
        if memory_index is not None and memory_retriever is not None:
            raise ValueError(
                "config: 'memory.index' is deprecated; "
                "select either memory.index or memory.retriever, not both"
            )

        return cls(
            event_transport=os.getenv(
                "EVENT_TRANSPORT",
                os.getenv(
                    "EVENT_BUS",
                    _expect_str(
                        event_transport.get("provider", "in-process"),
                        "event_transport.provider",
                    ),
                ),
            ),
            event_handler_timeout=_load_optional_positive_float(
                event.get("handlerTimeout"),
                os.getenv("EVENT_HANDLER_TIMEOUT"),
                "event.handlerTimeout",
            ),
            retry_policy=_load_optional_name(
                retry.get("default"),
                os.getenv("RETRY_POLICY"),
                "retry.default",
            ),
            retry_operation_policies=_load_retry_routes(retry.get("operations", {})),
            retry_max_attempts=_load_positive_int(
                retry.get("maxAttempts", 3),
                os.getenv("RETRY_MAX_ATTEMPTS"),
                "retry.maxAttempts",
            ),
            retry_max_delay=_load_nonnegative_float(
                retry.get("maxDelay", 2.0),
                os.getenv("RETRY_MAX_DELAY"),
                "retry.maxDelay",
            ),
            retry_total_timeout=_load_positive_float(
                retry.get("totalTimeout", 30.0),
                os.getenv("RETRY_TOTAL_TIMEOUT"),
                "retry.totalTimeout",
            ),
            model=os.getenv("AGENT_MODEL", _expect_str(raw.get("model", "deepseek"), "model")),
            model_fallback=_load_model_name_list(
                model_options.get("fallback", []),
                os.getenv("MODEL_FALLBACK"),
                "model.fallback",
                env_key="MODEL_FALLBACK",
            ),
            model_routes=_load_model_routes(model_options.get("routes", {})),
            model_keep_warm=_load_model_name_list(
                model_options.get("keepWarm", []),
                os.getenv("MODEL_KEEP_WARM"),
                "model.keepWarm",
                env_key="MODEL_KEEP_WARM",
            ),
            model_router=_load_optional_name(
                model_options.get("router"),
                os.getenv("MODEL_ROUTER"),
                "model.router",
            ),
            session_store=os.getenv(
                "SESSION_STORE", _expect_str(session.get("store", "jsonl"), "session.store")
            ),
            session_id=os.getenv(
                "SESSION_ID", _expect_str(session.get("id", "default"), "session.id")
            ),
            session_compaction=_load_optional_name(
                session.get("compaction"),
                os.getenv("SESSION_COMPACTION"),
                "session.compaction",
            ),
            memory_store=os.getenv(
                "MEMORY_STORE", _expect_str(memory.get("store", "sqlite"), "memory.store")
            ),
            memory_index=memory_index,
            memory_retriever=memory_retriever,
            memory_policy=_load_optional_name(
                memory.get("policy"),
                os.getenv("MEMORY_POLICY"),
                "memory.policy",
            ),
            memory_extractor=_load_optional_name(
                memory.get("extractor", "explicit"),
                os.getenv("MEMORY_EXTRACTOR"),
                "memory.extractor",
            ),
            memory_scope=os.getenv(
                "MEMORY_SCOPE",
                _expect_str(memory.get("scope", "user"), "memory.scope"),
            ),
            memory_owner_id=os.getenv(
                "MEMORY_OWNER_ID",
                _expect_text(memory.get("ownerId", ""), "memory.ownerId"),
            ),
            memory_agent_id=os.getenv(
                "MEMORY_AGENT_ID",
                _expect_text(memory.get("agentId", ""), "memory.agentId"),
            ),
            memory_tenant_id=os.getenv(
                "MEMORY_TENANT_ID",
                _expect_text(memory.get("tenantId", ""), "memory.tenantId"),
            ),
            embedding_provider=os.getenv(
                "EMBEDDING_PROVIDER",
                _expect_str(embedding.get("provider", "debug"), "embedding.provider"),
            ),
            context_max_tokens=int(
                os.getenv(
                    "CONTEXT_MAX_TOKENS",
                    _expect_int(context.get("maxTokens", 20000), "context.maxTokens"),
                )
            ),
            context_strategy=os.getenv(
                "CONTEXT_STRATEGY",
                _expect_str(context.get("strategy", "tail-window"), "context.strategy"),
            ),
            mcp_preload=_load_mcp_preload(mcp.get("preload", []), os.getenv("MCP_PRELOAD")),
            skill_preload=_load_skill_preload(
                skill.get("preload", []),
                os.getenv("SKILL_PRELOAD"),
            ),
            skill_max_content_bytes=_load_positive_int(
                skill.get("maxContentBytes", DEFAULT_SKILL_MAX_CONTENT_BYTES),
                os.getenv("SKILL_MAX_CONTENT_BYTES"),
                "skill.maxContentBytes",
            ),
            skill_max_preload_bytes=_load_positive_int(
                skill.get("maxPreloadBytes", DEFAULT_SKILL_MAX_PRELOAD_BYTES),
                os.getenv("SKILL_MAX_PRELOAD_BYTES"),
                "skill.maxPreloadBytes",
            ),
            skill_max_resources=_load_positive_int(
                skill.get("maxResources", DEFAULT_SKILL_MAX_RESOURCES),
                os.getenv("SKILL_MAX_RESOURCES"),
                "skill.maxResources",
            ),
            skill_max_resource_bytes=_load_positive_int(
                skill.get("maxResourceBytes", DEFAULT_SKILL_MAX_RESOURCE_BYTES),
                os.getenv("SKILL_MAX_RESOURCE_BYTES"),
                "skill.maxResourceBytes",
            ),
            skill_max_resource_total_bytes=_load_positive_int(
                skill.get(
                    "maxResourceTotalBytes",
                    DEFAULT_SKILL_MAX_RESOURCE_TOTAL_BYTES,
                ),
                os.getenv("SKILL_MAX_RESOURCE_TOTAL_BYTES"),
                "skill.maxResourceTotalBytes",
            ),
            skill_max_description_bytes=_load_positive_int(
                skill.get(
                    "maxDescriptionBytes",
                    DEFAULT_SKILL_MAX_DESCRIPTION_BYTES,
                ),
                os.getenv("SKILL_MAX_DESCRIPTION_BYTES"),
                "skill.maxDescriptionBytes",
            ),
            skill_max_resource_description_bytes=_load_positive_int(
                skill.get(
                    "maxResourceDescriptionBytes",
                    DEFAULT_SKILL_MAX_RESOURCE_DESCRIPTION_BYTES,
                ),
                os.getenv("SKILL_MAX_RESOURCE_DESCRIPTION_BYTES"),
                "skill.maxResourceDescriptionBytes",
            ),
            skill_max_listing_bytes=_load_positive_int(
                skill.get("maxListingBytes", DEFAULT_SKILL_MAX_LISTING_BYTES),
                os.getenv("SKILL_MAX_LISTING_BYTES"),
                "skill.maxListingBytes",
            ),
            skill_agent=os.getenv(
                "SKILL_AGENT",
                _expect_str(skill.get("agent", "default"), "skill.agent"),
            ),
            skill_permissions=_load_skill_permissions(
                skill.get("permissions", {}),
                "skill.permissions",
            ),
            skill_agent_permissions=_load_skill_agent_permissions(
                skill.get("agents", {}),
            ),
            skill_name_only=_load_skill_name_patterns(
                skill.get("nameOnly", []),
                os.getenv("SKILL_NAME_ONLY"),
                "skill.nameOnly",
            ),
            config_dir=config_path.parent.resolve(),
        )

    @property
    def event_bus(self) -> str:
        """Backward-compatible alias; the value is now an event transport."""
        return self.event_transport

    def plugin_config(self, kind: str, name: str) -> dict[str, Any]:
        """Return one plugin's private config by contribution kind and name."""
        if self.config_dir is None:
            return {}
        return load_plugin_config(self.config_dir, kind, name)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path}: invalid JSON config: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: config root must be a JSON object")
        return raw

    @staticmethod
    def _nested(raw: dict[str, Any], key: str) -> dict[str, Any]:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise ValueError(f"config: '{key}' must be an object")
        return value


def load_plugin_config(config_dir: str | Path, kind: str, name: str) -> dict[str, Any]:
    """Load config/plugins/<kind>/<name>.json with safe names and JSON validation."""
    if not _PLUGIN_NAME_RE.fullmatch(kind):
        raise ValueError(f"invalid plugin config kind: {kind!r}")
    if not _PLUGIN_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid plugin config name: {name!r}")

    plugin_dir = Path(config_dir) / "plugins" / kind
    path = plugin_dir / f"{name}.json"
    if not path.is_file() and "--" in name:
        path = plugin_dir / f"{name.split('--', 1)[0]}.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON plugin config: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: plugin config root must be a JSON object")
    return raw


def _merge_section_files(config_dir: Path, base: dict[str, Any]) -> dict[str, Any]:
    """Overlay optional per-domain files onto the shared config object."""
    merged = dict(base)
    for section, filename in _SECTION_FILES.items():
        path = config_dir / filename
        if not path.exists():
            continue
        section_raw = AppConfig._read_json(path)
        if section == "model":
            if "model" not in section_raw:
                raise ValueError(f"{path}: missing 'model'")
            merged["model"] = section_raw["model"]
            options = {key: value for key, value in section_raw.items() if key != "model"}
            merged["_model_options"] = {
                **_nested_copy(merged, "_model_options"),
                **options,
            }
            continue
        if section == "session":
            merged[section] = {**_nested_copy(merged, section), **section_raw}
            continue

        current = _nested_copy(merged, section)
        # Section files may use either {"store": ...} or {"<section>": {...}}.
        overlay = section_raw.get(section, section_raw)
        if not isinstance(overlay, dict):
            raise ValueError(f"{path}: config section must be a JSON object")
        merged[section] = {**current, **overlay}
    return merged


def _nested_copy(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"config: '{key}' must be an object")
    return dict(value)


def _expect_str(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config: '{key}' must be a non-empty string")
    return value


def _expect_text(value: Any, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"config: '{key}' must be a string")
    return value


def _expect_int(value: Any, key: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"config: '{key}' must be an integer")
    return value


def _load_mcp_preload(configured: Any, env_value: str | None) -> tuple[str, ...]:
    """Load and validate the explicit MCP startup list."""
    if env_value is not None:
        if not env_value.strip():
            return ()
        raw_names = env_value.split(",")
    else:
        if not isinstance(configured, list):
            raise ValueError("config: 'mcp.preload' must be a string array")
        raw_names = configured

    names: list[str] = []
    for index, value in enumerate(raw_names, start=1):
        key = f"mcp.preload[{index}]" if env_value is None else "MCP_PRELOAD"
        if not isinstance(value, str):
            raise ValueError(f"{key}: MCP plugin name must be a string")
        name = value.strip()
        if not name or not _PLUGIN_NAME_RE.fullmatch(name):
            raise ValueError(f"{key}: invalid MCP plugin name: {value!r}")
        if name in names:
            raise ValueError(f"{key}: duplicate MCP plugin name: {name}")
        names.append(name)
    return tuple(names)


def _load_skill_preload(configured: Any, env_value: str | None) -> tuple[str, ...]:
    """Load and validate the host-selected Skill startup list."""
    if env_value is not None:
        if not env_value.strip():
            return ()
        raw_names = env_value.split(",")
    else:
        if not isinstance(configured, list):
            raise ValueError("config: 'skill.preload' must be a string array")
        raw_names = configured

    names: list[str] = []
    for index, value in enumerate(raw_names, start=1):
        key = f"skill.preload[{index}]" if env_value is None else "SKILL_PRELOAD"
        if not isinstance(value, str):
            raise ValueError(f"{key}: skill plugin name must be a string")
        name = value.strip()
        if not name or not _PLUGIN_NAME_RE.fullmatch(name):
            raise ValueError(f"{key}: invalid skill plugin name: {value!r}")
        if name in names:
            raise ValueError(f"{key}: duplicate skill plugin name: {name}")
        names.append(name)
    return tuple(names)


def _load_skill_permissions(
    configured: Any,
    where: str,
) -> tuple[tuple[str, str], ...]:
    """Load pattern -> allow/ask/deny rules without executing plugin code."""
    if not isinstance(configured, dict):
        raise ValueError(f"config: '{where}' must be an object")

    rules: list[tuple[str, str]] = []
    for raw_pattern, raw_access in configured.items():
        if not isinstance(raw_pattern, str) or not raw_pattern.strip():
            raise ValueError(f"config: '{where}' keys must be non-empty strings")
        pattern = raw_pattern.strip()
        if not pattern.isprintable():
            raise ValueError(f"config: '{where}.{pattern}' must be printable text")
        if not isinstance(raw_access, str) or raw_access not in _SKILL_PERMISSIONS:
            raise ValueError(
                f"config: '{where}.{pattern}' must be one of {sorted(_SKILL_PERMISSIONS)}"
            )
        rules.append((pattern, raw_access))
    return tuple(rules)


def _load_skill_agent_permissions(
    configured: Any,
) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    if not isinstance(configured, dict):
        raise ValueError("config: 'skill.agents' must be an object")

    agents: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for raw_agent, raw_value in configured.items():
        if not isinstance(raw_agent, str) or not _SKILL_AGENT_RE.fullmatch(raw_agent):
            raise ValueError(f"config: 'skill.agents' has invalid agent name: {raw_agent!r}")
        if not isinstance(raw_value, dict):
            raise ValueError(f"config: 'skill.agents.{raw_agent}' must be an object")
        permissions = raw_value.get("permissions")
        if permissions is None and raw_value:
            raise ValueError(
                f"config: 'skill.agents.{raw_agent}' must contain a 'permissions' object"
            )
        agents.append(
            (
                raw_agent,
                _load_skill_permissions(
                    permissions if permissions is not None else {},
                    f"skill.agents.{raw_agent}.permissions",
                ),
            )
        )
    return tuple(agents)


def _load_skill_name_patterns(
    configured: Any,
    env_value: str | None,
    where: str,
) -> tuple[str, ...]:
    if env_value is not None:
        raw_patterns = [] if not env_value.strip() else env_value.split(",")
    else:
        if not isinstance(configured, list):
            raise ValueError(f"config: '{where}' must be a string array")
        raw_patterns = configured

    patterns: list[str] = []
    for index, value in enumerate(raw_patterns, start=1):
        key = f"{where}[{index}]" if env_value is None else "SKILL_NAME_ONLY"
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"config: '{key}' must be a non-empty string")
        pattern = value.strip()
        if not pattern.isprintable():
            raise ValueError(f"config: '{key}' must be printable text")
        if pattern in patterns:
            raise ValueError(f"config: '{key}' duplicate pattern: {pattern}")
        patterns.append(pattern)
    return tuple(patterns)


def _load_positive_int(configured: Any, env_value: str | None, key: str) -> int:
    raw = env_value if env_value is not None else configured
    if isinstance(raw, str) and env_value is not None:
        try:
            raw = int(raw)
        except ValueError as exc:
            raise ValueError(f"config: '{key}' must be an integer") from exc
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"config: '{key}' must be a positive integer")
    return raw


def _load_positive_float(configured: Any, env_value: str | None, key: str) -> float:
    raw = env_value if env_value is not None else configured
    if isinstance(raw, str) and env_value is not None:
        try:
            raw = float(raw)
        except ValueError as exc:
            raise ValueError(f"config: '{key}' must be a positive number") from exc
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ValueError(f"config: '{key}' must be a positive number")
    return float(raw)


def _load_nonnegative_float(configured: Any, env_value: str | None, key: str) -> float:
    raw = env_value if env_value is not None else configured
    if isinstance(raw, str) and env_value is not None:
        try:
            raw = float(raw)
        except ValueError as exc:
            raise ValueError(f"config: '{key}' must be a non-negative number") from exc
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
        raise ValueError(f"config: '{key}' must be a non-negative number")
    return float(raw)


def _load_optional_positive_float(
    configured: Any,
    env_value: str | None,
    key: str,
) -> float | None:
    raw = env_value if env_value is not None else configured
    if raw is None or raw == "":
        return None
    if isinstance(raw, str) and env_value is not None:
        try:
            raw = float(raw)
        except ValueError as exc:
            raise ValueError(f"config: '{key}' must be a positive number") from exc
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        raise ValueError(f"config: '{key}' must be a positive number")
    return float(raw)


def _load_model_name_list(
    configured: Any,
    env_value: str | None,
    key: str,
    *,
    env_key: str | None = None,
) -> tuple[str, ...]:
    if env_value is not None:
        if not env_value.strip():
            return ()
        raw_names = env_value.split(",")
    else:
        if not isinstance(configured, list):
            raise ValueError(f"config: '{key}' must be a string array")
        raw_names = configured

    names: list[str] = []
    for index, value in enumerate(raw_names, start=1):
        item_key = f"{key}[{index}]" if env_value is None else (env_key or key)
        if not isinstance(value, str):
            raise ValueError(f"{item_key}: model name must be a string")
        name = value.strip()
        if not name or not _PLUGIN_NAME_RE.fullmatch(name):
            raise ValueError(f"{item_key}: invalid model plugin name: {value!r}")
        if name in names:
            raise ValueError(f"{item_key}: duplicate model plugin name: {name}")
        names.append(name)
    return tuple(names)


def _load_model_routes(configured: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(configured, dict):
        raise ValueError("config: 'model.routes' must be an object")

    routes: list[tuple[str, tuple[str, ...]]] = []
    for role, candidates in configured.items():
        if not isinstance(role, str) or not role.strip():
            raise ValueError("config: 'model.routes' keys must be non-empty strings")
        role = role.strip()
        if role in {name for name, _ in routes}:
            raise ValueError(f"config: duplicate model route: {role}")
        routes.append(
            (
                role,
                _load_model_name_list(
                    candidates,
                    None,
                    f"model.routes.{role}",
                ),
            )
        )
    return tuple(routes)


def _load_retry_routes(configured: Any) -> tuple[tuple[str, str], ...]:
    if not isinstance(configured, dict):
        raise ValueError("config: 'retry.operations' must be an object")

    routes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_operation, raw_policy in configured.items():
        if not isinstance(raw_operation, str) or not raw_operation.strip():
            raise ValueError("config: 'retry.operations' keys must be non-empty strings")
        operation = raw_operation.strip()
        if operation in seen:
            raise ValueError(f"config: duplicate retry operation: {operation}")
        if not isinstance(raw_policy, str) or not _PLUGIN_NAME_RE.fullmatch(raw_policy):
            raise ValueError(f"config: 'retry.operations.{operation}' must be a valid plugin name")
        seen.add(operation)
        routes.append((operation, raw_policy))
    return tuple(routes)


def _load_optional_name(
    configured: Any,
    env_value: str | None,
    key: str,
) -> str | None:
    value = env_value if env_value is not None else configured
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _PLUGIN_NAME_RE.fullmatch(value):
        raise ValueError(f"config: '{key}' must be a valid plugin name")
    return value
