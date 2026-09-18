from pathlib import Path

import pytest

from plugins.loader import PluginManifest
from plugins.services import (
    DependencyCycleError,
    PluginRequirement,
    RuntimeServices,
    ServiceRef,
    ServiceResolutionError,
    candidate_service_names,
    resolve_dependency_order,
)


def _manifest(
    kind: str,
    name: str,
    *,
    requires: tuple[PluginRequirement, ...] = (),
    package_name: str | None = None,
    contribution_id: str | None = None,
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
        package_name=package_name,
        contribution_id=contribution_id,
    )


def test_runtime_services_resolve_selected_and_named_instances() -> None:
    services = RuntimeServices()
    services.select("embedding", "debug")
    provider = object()
    services.register("embedding", "debug", provider)

    assert services.get("embedding") is provider
    assert services.require("embedding", "debug") is provider
    assert services.available("embedding") == (("embedding", "debug"),)


def test_runtime_services_resolve_package_contribution_aliases() -> None:
    services = RuntimeServices()
    store = object()
    services.register(
        "memory",
        "memory-vector--vector",
        store,
        aliases=("memory-vector--vector", "vector", "memory-vector--vector"),
    )

    assert services.get("memory", "vector") is store
    assert services.get("memory", "memory-vector--vector") is store


def test_runtime_services_rejects_alias_collision_without_partial_registration() -> None:
    services = RuntimeServices()
    services.register("memory", "first", object(), aliases=("shared",))

    with pytest.raises(ServiceResolutionError, match="alias already registered"):
        services.register("memory", "second", object(), aliases=("shared",))

    assert services.get("memory", "second") is None


def test_candidate_service_names_include_package_and_contribution_names() -> None:
    manifest = PluginManifest(
        name="memory-vector--vector",
        type="memory",
        version="1.0.0",
        description="",
        enabled=True,
        directory=Path("."),
        entry={},
        contract="memory.v1",
        package_name="memory-vector",
        contribution_id="vector",
    )

    assert candidate_service_names(manifest) == (
        "memory-vector--vector",
        "vector",
    )


def test_dependency_graph_resolves_contribution_aliases_to_canonical_names() -> None:
    memory = _manifest(
        "memory",
        "memory-vector--vector",
        package_name="memory-vector",
        contribution_id="vector",
    )
    retriever = _manifest(
        "memory-retriever",
        "memory-vector--vector-native",
        requires=(PluginRequirement(kind="memory", name="vector"),),
        package_name="memory-vector",
        contribution_id="vector-native",
    )
    catalog = {
        ("memory", memory.name): memory,
        ("memory-retriever", retriever.name): retriever,
    }

    order = resolve_dependency_order(
        [ServiceRef("memory-retriever", "vector-native")],
        catalog=catalog,
        selected={"memory": "vector"},
    )

    assert order == [
        ServiceRef("memory", "memory-vector--vector"),
        ServiceRef("memory-retriever", "memory-vector--vector-native"),
    ]


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
