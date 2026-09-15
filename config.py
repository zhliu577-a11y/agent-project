# config.py - application and plugin configuration loader
#
# Layout:
#   config/config.json   shared runtime selections and defaults
#   config/model.json    model selection
#   config/session.json  session selection
#   config/memory.json   memory selection
#   config/embedding.json
#   config/context.json
#   config/mcp.json
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
_SECTION_FILES = {
    "model": "model.json",
    "session": "session.json",
    "memory": "memory.json",
    "embedding": "embedding.json",
    "context": "context.json",
    "mcp": "mcp.json",
}


@dataclass(frozen=True)
class AppConfig:
    model: str = "deepseek"
    model_fallback: tuple[str, ...] = ()
    model_routes: tuple[tuple[str, tuple[str, ...]], ...] = ()
    model_keep_warm: tuple[str, ...] = ()
    model_router: str | None = None
    session_store: str = "jsonl"
    session_id: str = "default"
    memory_store: str = "sqlite"
    embedding_provider: str = "debug"
    context_max_tokens: int = 20000
    context_strategy: str = "tail-window"
    mcp_preload: tuple[str, ...] = ()
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
        model_options = cls._nested(raw, "_model_options")

        return cls(
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
            memory_store=os.getenv(
                "MEMORY_STORE", _expect_str(memory.get("store", "sqlite"), "memory.store")
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
            config_dir=config_path.parent.resolve(),
        )

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
