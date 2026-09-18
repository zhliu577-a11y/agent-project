"""Built-in listener.v1 implementation for per-tool outcome metrics."""

import json
import os
from pathlib import Path

from core.events import Event, Subscription


def create_listener(plugin_dir):
    output_path = _output_path()
    state = {
        "total": 0,
        "ok": 0,
        "failed": 0,
        "denied": 0,
        "byTool": {},
    }
    denied_call_ids: set[str] = set()

    def on_denied(event: Event) -> None:
        tool_call = event.payload.get("tool_call")
        call_id = _call_id(tool_call)
        if call_id is not None:
            denied_call_ids.add(call_id)
        _increment(state, _tool_name(tool_call), "denied")
        _write(output_path, state)

    def on_after(event: Event) -> None:
        tool_call = event.payload.get("tool_call")
        call_id = _call_id(tool_call)
        if call_id is not None and call_id in denied_call_ids:
            denied_call_ids.discard(call_id)
            return
        outcome = "ok" if event.payload.get("ok") is True else "failed"
        _increment(state, _tool_name(tool_call), outcome)
        _write(output_path, state)

    return [
        Subscription(event="tool.denied", handler=on_denied, priority=0),
        Subscription(event="tool.after", handler=on_after, priority=0),
    ]


def _output_path() -> Path:
    configured = os.getenv("TOOL_METRICS_PATH")
    if configured:
        return Path(configured).expanduser()
    return Path.cwd() / "data" / "tool-metrics.json"


def _tool_name(tool_call: object) -> str:
    name = getattr(tool_call, "name", None)
    return name if isinstance(name, str) and name else "unknown"


def _call_id(tool_call: object) -> str | None:
    call_id = getattr(tool_call, "id", None)
    return call_id if isinstance(call_id, str) and call_id else None


def _increment(state: dict, tool_name: str, outcome: str) -> None:
    state["total"] += 1
    state[outcome] += 1
    tool = state["byTool"].setdefault(
        tool_name,
        {"total": 0, "ok": 0, "failed": 0, "denied": 0},
    )
    tool["total"] += 1
    tool[outcome] += 1


def _write(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
