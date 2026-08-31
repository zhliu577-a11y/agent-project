# core/events.py —— 事件总线：loop 广播事件，插件订阅事件
from collections import defaultdict

class EventBus:
    def __init__(self):
        self._listeners = defaultdict(list)

    def on(self, event: str, fn) -> None:
        self._listeners[event].append(fn)

    def emit(self, event: str, *args) -> None:
        for fn in self._listeners[event]:
            fn(*args)