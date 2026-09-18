from pathlib import Path

from core.context import SummaryWindowPolicy


def create_policy(plugin_dir: Path) -> SummaryWindowPolicy:
    return SummaryWindowPolicy()
