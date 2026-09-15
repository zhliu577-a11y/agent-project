from pathlib import Path

from core.context import TailWindowPolicy


def create_policy(plugin_dir: Path) -> TailWindowPolicy:
    return TailWindowPolicy()
