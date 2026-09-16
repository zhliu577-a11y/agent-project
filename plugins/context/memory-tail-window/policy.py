from pathlib import Path

from core.context import MemoryTailWindowPolicy


def create_policy(plugin_dir: Path, context=None) -> MemoryTailWindowPolicy:
    config = dict(getattr(context, "config", {}) or {})
    return MemoryTailWindowPolicy(memory_ratio=float(config.get("memoryRatio", 0.25)))
