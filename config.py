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
_SECTION_FILES = {
    "event": "event.json",
    "event_transport": "event_transport.json",
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
    memory_policy: str | None = None
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
        model_options = cls._nested(raw, "_model_options")

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
            memory_index=_load_optional_name(
                memory.get("index"),
                os.getenv("MEMORY_INDEX"),
                "memory.index",
            ),
            memory_policy=_load_optional_name(
                memory.get("policy"),
                os.getenv("MEMORY_POLICY"),
                "memory.policy",
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
