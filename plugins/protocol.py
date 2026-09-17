# plugins/protocol.py - plugin platform protocol versions and capability contracts
from dataclasses import dataclass

MANIFEST_API_VERSION = "1"
PLUGIN_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PLUGIN_PROTOCOL_VERSION})
SKILL_PROTOCOL_VERSION = 1
SKILL_PROTOCOL_VERSIONS = (1, 2)


@dataclass(frozen=True)
class CapabilityContract:
    """The host-side interface a contribution promises to implement."""

    kind: str
    protocol_version: int

    @property
    def name(self) -> str:
        return f"{self.kind}.v{self.protocol_version}"


def capability_contract(kind: str, protocol_version: int) -> CapabilityContract:
    if (
        isinstance(protocol_version, bool)
        or not isinstance(protocol_version, int)
        or protocol_version <= 0
    ):
        raise ValueError(
            f"plugin protocolVersion must be a positive integer, got {protocol_version!r}"
        )
    return CapabilityContract(kind=kind, protocol_version=protocol_version)
