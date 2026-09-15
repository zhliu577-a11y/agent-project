# plugins/listeners/timeline/listener.py —— listener 插件示例：事件时间线
#
# listener 插件契约：工厂接收插件目录，返回 core.events.Subscription 列表
# （事件名 + handler + priority）。loader 会把它们注册进事件总线；
# 决策类事件（如 user_prompt.submit）不允许 listener 订阅——那里走 hook 契约。
import json
from pathlib import Path

from core.events import Event, Subscription


def create_listener(plugin_dir):
    """返回订阅列表：把所有观察事件追加写入本插件目录的 timeline.jsonl。"""
    log_path = Path(plugin_dir) / "timeline.jsonl"

    def on_event(event: Event) -> None:
        record = {
            "ts": event.ts,
            "trace_id": event.trace_id,
            "name": event.name,
            "payload": {key: _short(value) for key, value in event.payload.items()},
        }
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")

    return [Subscription(event="*", handler=on_event, priority=0)]


def _short(value, limit: int = 120) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:limit]
