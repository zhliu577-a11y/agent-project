# plugins/hooks/permission/hook.py —— 示例钩子插件：工具权限策略
#
# 一个钩子插件 = 实现 LifecycleHooks 的类 + 一个工厂函数。
# 工厂接收插件目录（Path），返回钩子实例；清单里声明：
#   "entry": { "module": "hook.py", "factory": "create_hook" }
import fnmatch
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from core.hooks import LifecycleHooks
from core.types import ToolCall, TurnContext

logger = logging.getLogger(__name__)

_VALID_MODES = {"allow", "ask", "deny"}


@dataclass
class Rule:
    pattern: str  # 支持通配符，如 "filesystem__*"
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
            logger.warning("已拦截工具调用: %s（策略拒绝）", tool_call.name)
            return False
        # mode == "ask"：交互提示必须走 input（不能进日志，用户要看得见并回答）
        logger.info("请求用户确认工具调用: %s", tool_call.name)
        answer = (
            input(f"[权限] 是否允许调用 {tool_call.name}(参数: {tool_call.arguments})? [y/N]: ")
            .strip()
            .lower()
        )
        return answer in {"y", "yes"}


def load_policy(path: str | Path) -> PermissionHooks:
    """读取并校验权限策略 JSON。"""
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 顶层必须是 JSON 对象")

    default = raw.get("default", "allow")
    if default not in _VALID_MODES:
        raise ValueError(f"{path}: 非法的 default '{default}'，可选: {sorted(_VALID_MODES)}")

    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ValueError(f"{path}: 'rules' 必须是数组")

    rules: list[Rule] = []
    for i, rule in enumerate(rules_raw, start=1):
        where = f"{path}: 第 {i} 条规则"
        if not isinstance(rule, dict):
            raise ValueError(f"{where} 必须是对象")
        tool = rule.get("tool")
        mode = rule.get("mode")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError(f"{where} 缺少非空 'tool'")
        if mode not in _VALID_MODES:
            raise ValueError(
                f"{where} ('{tool}'): 非法的 mode '{mode}'，可选: {sorted(_VALID_MODES)}"
            )
        rules.append(Rule(tool, mode))

    return PermissionHooks(rules, default=default)


def create_hook(plugin_dir: Path) -> PermissionHooks:
    """插件工厂：加载本插件目录下的 permission.json 并返回策略实例。"""
    return load_policy(plugin_dir / "permission.json")
