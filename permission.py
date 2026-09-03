# permission.py —— 工具权限策略（配置驱动）
import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path

from core.hooks import LifecycleHooks
from core.types import ToolCall, TurnContext

_VALID_MODES = {"allow", "ask", "deny"}


@dataclass
class Rule:
    pattern: str  # 支持通配符，如 "delete_*"
    mode: str  # allow | ask | deny


class PermissionHooks(LifecycleHooks):
    """在 tool_before 时按策略决定：允许 / 询问用户 / 拒绝。"""

    def __init__(self, rules: list[Rule] | None = None, default: str = "allow") -> None:
        if default not in _VALID_MODES:
            raise ValueError(f"非法的 default 模式: {default}")
        for rule in rules or []:
            if rule.mode not in _VALID_MODES:
                raise ValueError(f"非法的规则模式: {rule.mode}")
        self._rules = list(rules or [])
        self._default = default

    def mode_for(self, tool_name: str) -> str:
        for rule in self._rules:
            if fnmatch.fnmatch(tool_name, rule.pattern):
                return rule.mode
        return self._default

    async def tool_before(self, ctx: TurnContext, tool_call: ToolCall) -> bool:
        mode = self.mode_for(tool_call.name)
        if mode == "allow":
            return True
        if mode == "deny":
            print(f"[权限] 已拦截工具调用: {tool_call.name}（策略拒绝）")
            return False
        # mode == "ask"：在终端询问用户
        answer = (
            input(f"[权限] 是否允许调用 {tool_call.name}(参数: {tool_call.arguments})? [y/N]: ")
            .strip()
            .lower()
        )
        return answer in {"y", "yes"}


def load_policy(path: str | Path = "permission.json") -> PermissionHooks:
    """从 JSON 读取权限策略。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = [Rule(rule["tool"], rule["mode"]) for rule in raw.get("rules", [])]
    return PermissionHooks(rules, default=raw.get("default", "allow"))
