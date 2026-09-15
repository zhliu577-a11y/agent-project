# plugins/context.py - process-local plugin dependency and configuration view
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class PluginContext:
    """Stable host-provided context passed to internal plugin factories.

    Plugins still receive their own directory for backward compatibility. New
    plugins may also declare a ``context`` keyword argument to receive this
    object and read centrally managed configuration.
    """

    name: str
    kind: str
    directory: Path
    config: Mapping[str, Any]
    package_name: str | None = None
    contribution_id: str | None = None
    protocol_version: int = 1
    contract: str = ""

    @classmethod
    def create(
        cls,
        *,
        name: str,
        kind: str,
        directory: Path,
        config: dict[str, Any] | None = None,
        package_name: str | None = None,
        contribution_id: str | None = None,
        protocol_version: int = 1,
        contract: str = "",
    ) -> "PluginContext":
        return cls(
            name=name,
            kind=kind,
            directory=directory,
            config=MappingProxyType(dict(config or {})),
            package_name=package_name,
            contribution_id=contribution_id,
            protocol_version=protocol_version,
            contract=contract,
        )
