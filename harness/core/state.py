# core/state.py —— 状态正式化：TurnContext 的 JSON 安全快照
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from core.types import TurnContext


def capture(ctx: TurnContext) -> dict[str, Any]:
    """把一次运行的收尾状态转成 JSON 安全快照（不含消息历史）。"""
    return {
        "turn": ctx.turn,
        "stop_reason": ctx.stop_reason,
        "state": _json_safe(ctx.state),
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
