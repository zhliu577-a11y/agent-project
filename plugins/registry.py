# plugins/registry.py - persistent host state for installed plugin packages
import json
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

REGISTRY_API_VERSION = 1


@dataclass(frozen=True)
class PluginRecord:
    """Host-owned lifecycle state for one plugin package."""

    name: str
    version: str
    path: str
    digest: str
    source: str
    installed: bool = True
    enabled: bool = False
    installed_at: str = ""
    updated_at: str = ""
    runtime_status: str = "idle"
    last_error: str | None = None

    @classmethod
    def from_dict(cls, name: str, raw: object, where: str) -> "PluginRecord":
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: plugin record must be an object")

        version = _expect_str(raw.get("version", ""), where, "version", allow_empty=True)
        path = _expect_str(raw.get("path"), where, "path")
        digest = _expect_str(raw.get("digest", ""), where, "digest", allow_empty=True)
        source = _expect_str(raw.get("source", "unknown"), where, "source")
        installed = _expect_bool(raw.get("installed", True), where, "installed")
        enabled = _expect_bool(raw.get("enabled", False), where, "enabled")
        installed_at = _expect_str(
            raw.get("installedAt", ""), where, "installedAt", allow_empty=True
        )
        updated_at = _expect_str(raw.get("updatedAt", ""), where, "updatedAt", allow_empty=True)
        runtime_status = _expect_str(raw.get("runtimeStatus", "idle"), where, "runtimeStatus")
        last_error = raw.get("lastError")
        if last_error is not None and not isinstance(last_error, str):
            raise ValueError(f"{where}: 'lastError' must be a string or null")

        return cls(
            name=name,
            version=version,
            path=path,
            digest=digest,
            source=source,
            installed=installed,
            enabled=enabled,
            installed_at=installed_at,
            updated_at=updated_at,
            runtime_status=runtime_status,
            last_error=last_error,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "installed": self.installed,
            "enabled": self.enabled,
            "version": self.version,
            "source": self.source,
            "path": self.path,
            "digest": self.digest,
            "installedAt": self.installed_at,
            "updatedAt": self.updated_at,
            "runtimeStatus": self.runtime_status,
            "lastError": self.last_error,
        }

    def with_enabled(self, enabled: bool, *, updated_at: str) -> "PluginRecord":
        return replace(self, enabled=enabled, updated_at=updated_at)


class PluginRegistry:
    """Atomic JSON registry for installed package lifecycle state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._lock = threading.RLock()
        self._records: dict[str, PluginRecord] = {}
        self._load()

    def get(self, name: str) -> PluginRecord | None:
        with self._lock:
            return self._records.get(name)

    def all(self) -> tuple[PluginRecord, ...]:
        with self._lock:
            return tuple(self._records[name] for name in sorted(self._records))

    def set(self, record: PluginRecord) -> None:
        with self._lock:
            updated = dict(self._records)
            updated[record.name] = record
            self._write(updated)
            self._records = updated

    def remove(self, name: str) -> bool:
        with self._lock:
            if name not in self._records:
                return False
            updated = dict(self._records)
            del updated[name]
            self._write(updated)
            self._records = updated
            return True

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{self.path}: invalid plugin registry JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{self.path}: plugin registry root must be an object")
        if raw.get("apiVersion") != REGISTRY_API_VERSION:
            raise ValueError(
                f"{self.path}: unsupported registry apiVersion: {raw.get('apiVersion')!r}"
            )
        plugins = raw.get("plugins", {})
        if not isinstance(plugins, dict):
            raise ValueError(f"{self.path}: 'plugins' must be an object")

        records: dict[str, PluginRecord] = {}
        for name, value in plugins.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{self.path}: plugin names must be non-empty strings")
            records[name] = PluginRecord.from_dict(
                name,
                value,
                f"{self.path}: plugins.{name}",
            )
        self._records = records

    def _write(self, records: dict[str, PluginRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "apiVersion": REGISTRY_API_VERSION,
            "plugins": {name: record.to_dict() for name, record in sorted(records.items())},
        }
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def _expect_str(value: object, where: str, key: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{where}: '{key}' must be a string")
    return value


def _expect_bool(value: object, where: str, key: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{where}: '{key}' must be a boolean")
    return value
