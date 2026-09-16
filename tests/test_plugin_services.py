from pathlib import Path

import pytest

from plugins.loader import PluginManifest
from plugins.services import (
    DependencyCycleError,
    PluginRequirement,
    RuntimeServices,
    ServiceRef,
    ServiceResolutionError,
    resolve_dependency_order,
)


def _manifest(
    kind: str,
    name: str,
    *,
    requires: tuple[PluginRequirement, ...] = (),
) -> PluginManifest:
    return PluginManifest(
        name=name,
        type=kind,
        version="1.0.0",
        description="",
        enabled=True,
        directory=Path("."),
        entry={},
        contract=f"{kind}.v1",
        requires=requires,
    )


def test_runtime_services_resolve_selected_and_named_instances() -> None:
    services = RuntimeServices()
    services.select("embedding", "debug")
    provider = object()
    services.register("embedding", "debug", provider)

    assert services.get("embedding") is provider
    assert services.require("embedding", "debug") is provider
    assert services.available("embedding") == (("embedding", "debug"),)


def test_dependency_graph_is_dependency_first() -> None:
    vector = _manifest(
        "memory",
        "vector",
        requires=(PluginRequirement(kind="embedding", inject="embedding"),),
    )
    embedding = _manifest("embedding", "debug")
    catalog = {
        ("memory", "vector"): vector,
        ("embedding", "debug"): embedding,
    }

    order = resolve_dependency_order(
        [ServiceRef("memory", "vector")],
        catalog=catalog,
        selected={"embedding": "debug"},
    )

    assert order == [
        ServiceRef("embedding", "debug"),
        ServiceRef("memory", "vector"),
    ]


def test_dependency_graph_rejects_missing_required_service() -> None:
    vector = _manifest(
        "memory",
        "vector",
        requires=(PluginRequirement(kind="embedding"),),
    )

    with pytest.raises(ServiceResolutionError, match="no selected service"):
        resolve_dependency_order(
            [ServiceRef("memory", "vector")],
            catalog={("memory", "vector"): vector},
            selected={},
        )


def test_dependency_graph_detects_cycles() -> None:
    first = _manifest(
        "memory",
        "first",
        requires=(PluginRequirement(kind="memory", name="second"),),
    )
    second = _manifest(
        "memory",
        "second",
        requires=(PluginRequirement(kind="memory", name="first"),),
    )
    catalog = {
        ("memory", "first"): first,
        ("memory", "second"): second,
    }

    with pytest.raises(DependencyCycleError, match="dependency cycle"):
        resolve_dependency_order(
            [ServiceRef("memory", "first")],
            catalog=catalog,
            selected={},
        )
