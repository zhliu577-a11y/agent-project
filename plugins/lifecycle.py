# plugins/lifecycle.py - deterministic setup/start/stop lifecycle runner
import inspect
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from plugins.context import PluginContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginLifecycleRecord:
    name: str
    context: PluginContext
    instance: object
    started: bool = False


class PluginLifecycleManager:
    """Invoke optional setup/start/stop hooks with reverse-order rollback."""

    def __init__(self) -> None:
        self._records: list[PluginLifecycleRecord] = []

    def add(self, context: PluginContext, instance: object) -> None:
        self._records.append(
            PluginLifecycleRecord(
                name=context.name,
                context=context,
                instance=instance,
            )
        )

    async def start_all(self) -> None:
        started: list[PluginLifecycleRecord] = []
        try:
            for index, record in enumerate(self._records):
                # Mark before setup so a partially initialized plugin receives
                # stop() when either setup() or start() fails.
                updated = PluginLifecycleRecord(
                    name=record.name,
                    context=record.context,
                    instance=record.instance,
                    started=True,
                )
                self._records[index] = updated
                started.append(updated)
                if callable(getattr(record.instance, "setup", None)):
                    await _call(record.instance.setup, record.context)
                if callable(getattr(record.instance, "start", None)):
                    await _call(record.instance.start)
        except Exception:
            await self._stop_records(reversed(started))
            self._mark_stopped()
            raise

    async def stop_all(self) -> None:
        await self._stop_records(reversed(self._records))
        self._mark_stopped()

    def _mark_stopped(self) -> None:
        for index, record in enumerate(self._records):
            self._records[index] = PluginLifecycleRecord(
                name=record.name,
                context=record.context,
                instance=record.instance,
                started=False,
            )

    async def _stop_records(
        self,
        records: Iterable[PluginLifecycleRecord],
    ) -> None:
        for record in records:
            if not record.started:
                continue
            stop = getattr(record.instance, "stop", None)
            if not callable(stop):
                continue
            try:
                await _call(stop)
            except Exception:
                logger.exception("plugin lifecycle stop failed: %s", record.name)


async def _call(callback: Callable[..., Any], *args: object) -> None:
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
