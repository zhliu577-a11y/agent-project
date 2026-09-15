# plugins/protocol.py - plugin platform protocol versions and capability contracts
from dataclasses import dataclass

MANIFEST_API_VERSION = "1"
PLUGIN_PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({PLUGIN_PROTOCOL_VERSION})


@dataclass(frozen=True)
class CapabilityContract:
    """The host-side interface a contribution promises to implement."""

    kind: str
    protocol_version: int

    @property
    def name(self) -> str:
        return f"{self.kind}.v{self.protocol_version}"


def capability_contract(kind: str, protocol_version: int) -> CapabilityContract:
    if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ValueError(
            f"unsupported plugin protocolVersion {protocol_version!r}; "
            f"supported: {sorted(SUPPORTED_PROTOCOL_VERSIONS)}"
        )
    return CapabilityContract(kind=kind, protocol_version=protocol_version)
