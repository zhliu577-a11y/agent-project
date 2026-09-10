# core/events.py —— 事件总线 UNBOX：观察广播 + 决策折叠（ADR 0006）
#
# - publish：观察类事件，只读广播、异常隔离、支持 subscribe("*")
# - decide ：决策类事件，订阅者返回 allow/ask/deny，按 deny > ask > allow 折叠
# - 事件默认带当前 trace_id；EVENT_LOG 场景可用 jsonl_sink 落盘
import inspect
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from core.tracing import current_trace_id

logger = logging.getLogger(__name__)

Decision = Literal["allow", "ask", "deny"]
Handler = Callable[["Event"], Any]
WILDCARD = "*"

# 决策类事件：只允许内核 / hook 契约参与，listener 插件不得订阅
DECISION_EVENTS = frozenset({"user_prompt.submit"})


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


@dataclass(frozen=True)
class _Subscriber:
    priority: int
    order: int
    handler: Handler


@dataclass(frozen=True)
class Subscription:
    """插件（listener kind）声明的订阅项：事件名 + 处理器 + 优先级。"""

    event: str
    handler: Handler
    priority: int = 0


def coerce_subscriptions(value: Any, where: str) -> tuple[Subscription, ...]:
    """把工厂产物规范成 Subscription 元组（允许单个或列表）。"""
    if isinstance(value, Subscription):
        items: Iterable[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise ValueError(
            f"{where}: 工厂必须返回 Subscription 或 Subscription 列表，"
            f"实际是 {type(value).__name__}"
        )
    subscriptions = tuple(items)
    for item in subscriptions:
        if not isinstance(item, Subscription):
            raise ValueError(f"{where}: 列表里混入了非 Subscription 对象: {type(item).__name__}")
        if item.event in DECISION_EVENTS:
            raise ValueError(
                f"{where}: listener 插件不得订阅决策类事件 '{item.event}'（决策走 hook 契约）"
            )
    if not subscriptions:
        raise ValueError(f"{where}: 工厂没有返回任何 Subscription")
    return subscriptions


async def _invoke(handler: Handler, event: Event) -> Any:
    """调用处理器，兼容同步/异步（同步返回值原样返回）。"""
    result = handler(event)
    if inspect.isawaitable(result):
        return await result
    return result


class EventBus:
    """进程内事件总线：观察广播 + 决策折叠。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[_Subscriber]] = {}
        self._counter = 0

    def subscribe(self, name: str, handler: Handler, *, priority: int = 0) -> Callable[[], None]:
        """订阅事件；name 传 "*" 表示订阅全部（观察类）。

        返回退订函数（为热卸载/测试准备）。
        """
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TypeError(f"priority 必须是整数，收到: {priority!r}")
        self._counter += 1
        subscriber = _Subscriber(priority, self._counter, handler)
        self._subs.setdefault(name, []).append(subscriber)

        def unsubscribe() -> None:
            entries = self._subs.get(name)
            if not entries:
                return
            self._subs[name] = [item for item in entries if item is not subscriber]

        return unsubscribe

    def subscriber_count(self, name: str) -> int:
        return len(self._subs.get(name, []))

    def _ordered(self, name: str) -> list[_Subscriber]:
        merged = [*self._subs.get(name, []), *self._subs.get(WILDCARD, [])]
        return sorted(merged, key=lambda sub: (sub.priority, sub.order))

    async def publish(self, event: Event) -> None:
        """观察类广播：按优先级通知全部订阅者，异常只记录不影响主流程。"""
        event = self._with_trace(event)
        for subscriber in self._ordered(event.name):
            try:
                await _invoke(subscriber.handler, event)
            except Exception as exc:
                logger.exception("事件订阅者处理失败 name=%s: %s", event.name, exc)

    async def decide(self, event: Event) -> Decision:
        """决策类事件：汇总表态，deny > ask > allow；异常按 deny 处理。"""
        event = self._with_trace(event)
        decision: Decision = "allow"
        for subscriber in self._ordered(event.name):
            try:
                vote = await _invoke(subscriber.handler, event)
            except Exception as exc:
                logger.exception("决策订阅者执行失败（按 deny 处理）name=%s: %s", event.name, exc)
                vote = "deny"
            if vote is None:
                continue
            if vote not in ("allow", "ask", "deny"):
                logger.warning("决策订阅者返回非法表态 %r（按 deny 处理）", vote)
                vote = "deny"
            if vote == "deny":
                decision = "deny"
            elif decision == "allow" and vote == "ask":
                decision = "ask"
        return decision

    @staticmethod
    def _with_trace(event: Event) -> Event:
        if event.trace_id is not None:
            return event
        return replace(event, trace_id=current_trace_id())


def _safe_value(value: Any, limit: int = 200) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_value(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, limit) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value[:limit] if isinstance(value, str) else value
    text = repr(value)
    return text[:limit]


def jsonl_sink(path: str | Path) -> Handler:
    """构造一个把事件写成 JSONL 的观察订阅者（用于 EVENT_LOG 时间线）。"""
    sink_path = Path(path)

    def handler(event: Event) -> None:
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": event.ts,
                "trace_id": event.trace_id,
                "name": event.name,
                "payload": _safe_value(event.payload),
            },
            ensure_ascii=False,
        )
        with sink_path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")

    return handler
