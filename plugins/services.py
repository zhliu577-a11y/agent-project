"""Runtime service registry and dependency graph resolution.

The loader validates plugin contracts. This module is responsible for the
next layer: resolving declared service dependencies and handing already
constructed instances to plugin factories.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ServiceResolutionError(RuntimeError):
    """A requested runtime service cannot be resolved."""


class DependencyCycleError(ServiceResolutionError):
    """The requested service graph contains a cycle."""


@dataclass(frozen=True)
class PluginRequirement:
    """One service dependency declared by a plugin manifest."""

    kind: str
    name: str | None = None
    contract: str | None = None
    required: bool = True
    inject: str | None = None

    @property
    def injection_name(self) -> str:
        return self.inject or self.kind.replace("-", "_")


@dataclass(frozen=True)
class ServiceRef:
    """A concrete plugin instance in the runtime service graph."""

    kind: str
    name: str


@runtime_checkable
class ServiceManifest(Protocol):
    """The subset of PluginManifest needed by dependency resolution."""

    name: str
    type: str
    contract: str
    package_name: str | None
    contribution_id: str | None
    requires: tuple[PluginRequirement, ...]


class RuntimeServices:
    """Process-local registry shared by all plugin contexts in one Runtime."""

    def __init__(self) -> None:
        self._selected: dict[str, str] = {}
        self._instances: dict[tuple[str, str], object] = {}

    def select(self, kind: str, name: str) -> None:
        self._selected[kind] = name

    def selected_name(self, kind: str) -> str | None:
        return self._selected.get(kind)

    def register(self, kind: str, name: str, instance: object) -> None:
        key = (kind, name)
        if key in self._instances:
            raise ServiceResolutionError(f"service already registered: {kind}/{name}")
        self._instances[key] = instance

    def unregister(self, kind: str, name: str) -> None:
        self._instances.pop((kind, name), None)

    def get(self, kind: str, name: str | None = None) -> object | None:
        resolved_name = name or self._selected.get(kind)
        if resolved_name is None:
            return None
        return self._instances.get((kind, resolved_name))

    def require(self, kind: str, name: str | None = None) -> object:
        resolved_name = name or self._selected.get(kind)
        if resolved_name is None:
            raise ServiceResolutionError(f"no selected service for kind '{kind}'")
        service = self._instances.get((kind, resolved_name))
        if service is None:
            raise ServiceResolutionError(f"service not initialized: {kind}/{resolved_name}")
        return service

    def has(self, kind: str, name: str | None = None) -> bool:
        return self.get(kind, name) is not None

    def available(self, kind: str | None = None) -> tuple[tuple[str, str], ...]:
        items = self._instances
        if kind is not None:
            items = {key: value for key, value in items.items() if key[0] == kind}
        return tuple(sorted(items))


def _candidate_names(manifest: ServiceManifest) -> tuple[str, ...]:
    names = [manifest.name]
    if manifest.contribution_id:
        names.append(manifest.contribution_id)
    if manifest.package_name and manifest.contribution_id:
        names.append(f"{manifest.package_name}--{manifest.contribution_id}")
    # Preserve order while removing duplicates.
    return tuple(dict.fromkeys(names))


def _find_manifest(
    catalog: Mapping[tuple[str, str], ServiceManifest],
    *,
    kind: str,
    name: str,
) -> ServiceManifest | None:
    exact = catalog.get((kind, name))
    if exact is not None:
        return exact
    matches = [
        manifest
        for (candidate_kind, _), manifest in catalog.items()
        if candidate_kind == kind and name in _candidate_names(manifest)
    ]
    if len(matches) > 1:
        raise ServiceResolutionError(f"ambiguous service requirement: {kind}/{name}")
    return matches[0] if matches else None


def _resolve_requirement(
    requirement: PluginRequirement,
    *,
    catalog: Mapping[tuple[str, str], ServiceManifest],
    selected: Mapping[str, str],
) -> ServiceRef | None:
    name = requirement.name or selected.get(requirement.kind)
    if name is None:
        if requirement.required:
            raise ServiceResolutionError(
                f"no selected service for required kind '{requirement.kind}'"
            )
        return None

    manifest = _find_manifest(catalog, kind=requirement.kind, name=name)
    if manifest is None:
        if requirement.required:
            raise ServiceResolutionError(f"required service not found: {requirement.kind}/{name}")
        return None
    if requirement.contract is not None and requirement.contract != manifest.contract:
        raise ServiceResolutionError(
            f"service contract mismatch for {requirement.kind}/{name}: "
            f"required {requirement.contract}, found {manifest.contract}"
        )
    return ServiceRef(kind=requirement.kind, name=manifest.name)


def resolve_dependency_order(
    roots: Sequence[ServiceRef],
    *,
    catalog: Mapping[tuple[str, str], ServiceManifest],
    selected: Mapping[str, str],
) -> list[ServiceRef]:
    """Return roots and dependencies in dependency-first order."""

    ordered: list[ServiceRef] = []
    visiting: list[ServiceRef] = []
    visited: set[ServiceRef] = set()

    def visit(ref: ServiceRef) -> None:
        if ref in visited:
            return
        if ref in visiting:
            cycle = [*visiting[visiting.index(ref) :], ref]
            path = " -> ".join(f"{item.kind}/{item.name}" for item in cycle)
            raise DependencyCycleError(f"plugin dependency cycle: {path}")

        manifest = _find_manifest(catalog, kind=ref.kind, name=ref.name)
        if manifest is None:
            raise ServiceResolutionError(f"service not found: {ref.kind}/{ref.name}")

        visiting.append(ref)
        for requirement in manifest.requires:
            dependency = _resolve_requirement(
                requirement,
                catalog=catalog,
                selected=selected,
            )
            if dependency is not None:
                visit(dependency)
        visiting.pop()
        visited.add(ref)
        ordered.append(ref)

    for root in roots:
        visit(root)
    return ordered


_PYTHON_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_requirement(requirement: PluginRequirement, where: str) -> None:
    """Validate fields that must remain safe for host-side factory injection."""

    if not _PYTHON_IDENTIFIER.fullmatch(requirement.kind):
        raise ValueError(f"{where}: dependency kind must be a valid identifier")
    if requirement.name is not None and not requirement.name.strip():
        raise ValueError(f"{where}: dependency name must not be empty")
    if requirement.contract is not None and not requirement.contract.strip():
        raise ValueError(f"{where}: dependency contract must not be empty")
    if requirement.inject is not None and not _PYTHON_IDENTIFIER.fullmatch(requirement.inject):
        raise ValueError(f"{where}: dependency inject must be a valid Python identifier")


def requirement_debug_value(requirement: PluginRequirement) -> dict[str, Any]:
    """Return a JSON-safe representation used by diagnostics."""

    return {
        "kind": requirement.kind,
        "name": requirement.name,
        "contract": requirement.contract,
        "required": requirement.required,
        "inject": requirement.inject,
    }
