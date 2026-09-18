# plugins/hooks/permission/hook.py - declarative tool permission hook
import fnmatch
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from core.hooks import HookDecision, LifecycleHooks
from core.types import ToolCall, TurnContext
from plugins.context import PluginContext

logger = logging.getLogger(__name__)

_VALID_MODES = {"allow", "ask", "deny"}


@dataclass
class Rule:
    pattern: str
    mode: str


class PermissionHooks(LifecycleHooks):
    """Return allow/ask/deny decisions for matching tool names."""

    def __init__(self, rules: list[Rule] | None = None, default: str = "allow") -> None:
        if default not in _VALID_MODES:
            raise ValueError(f"invalid default mode: {default}")
        for rule in rules or []:
            if rule.mode not in _VALID_MODES:
                raise ValueError(f"invalid rule mode: {rule.mode}")
        self._rules = list(rules or [])
        self._default = default

    def mode_for(self, tool_name: str) -> str:
        for rule in self._rules:
            if fnmatch.fnmatch(tool_name, rule.pattern):
                return rule.mode
        return self._default

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> HookDecision:
        mode = self.mode_for(tool_call.name)
        if mode == "deny":
            logger.warning("tool call denied by policy: %s", tool_call.name)
        elif mode == "ask":
            logger.info("tool call requires confirmation: %s", tool_call.name)
        return mode  # type: ignore[return-value]


def load_policy(path: str | Path) -> PermissionHooks:
    """Load permission policy from a standalone JSON file."""
    policy_path = Path(path)
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    return parse_policy(raw, str(policy_path))


def parse_policy(raw: object, where: str = "permission policy") -> PermissionHooks:
    """Validate a policy object from either the plugin or central config."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: top-level must be a JSON object")

    default = raw.get("default", "allow")
    if default not in _VALID_MODES:
        raise ValueError(f"{where}: invalid default '{default}', choose {sorted(_VALID_MODES)}")

    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ValueError(f"{where}: 'rules' must be an array")

    rules: list[Rule] = []
    for index, item in enumerate(rules_raw, start=1):
        item_where = f"{where}: rule {index}"
        if not isinstance(item, dict):
            raise ValueError(f"{item_where} must be an object")
        tool = item.get("tool")
        mode = item.get("mode")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"{item_where} requires a non-empty 'tool'")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"{item_where} ('{tool}'): invalid mode '{mode}', choose {sorted(_VALID_MODES)}"
            )
        rules.append(Rule(tool, str(mode)))

    return PermissionHooks(rules, default=str(default))


def create_hook(
    plugin_dir: Path,
    context: PluginContext | None = None,
) -> PermissionHooks:
    """Prefer central config, with the plugin-local file as compatibility fallback."""
    if context is not None and context.config:
        return parse_policy(dict(context.config), f"plugin config hook/{context.name}")
    return load_policy(plugin_dir / "permission.json")
