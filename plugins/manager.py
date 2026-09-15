# plugins/manager.py - controlled plugin package lifecycle
import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from plugins.loader import PackageInspection, inspect_package
from plugins.registry import PluginRecord, PluginRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PLUGIN_DIR = PROJECT_ROOT / "plugins"
DEFAULT_PLUGIN_REGISTRY_PATH = PROJECT_ROOT / "data" / "plugin-registry.json"
DEFAULT_PLUGIN_STORE_DIR = PROJECT_ROOT / "data" / "plugin-store"

MAX_PACKAGE_FILES = 2000
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
_IGNORED_PARTS = {".git", ".pytest_cache", ".ruff_cache", "__pycache__"}
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class PluginManager:
    """Validate, install, enable, disable, and remove plugin packages."""

    def __init__(
        self,
        *,
        registry_path: str | Path | None = None,
        store_dir: str | Path | None = None,
        builtin_dir: str | Path | None = None,
    ) -> None:
        self.registry_path = Path(registry_path or DEFAULT_PLUGIN_REGISTRY_PATH).resolve()
        self.store_dir = Path(store_dir or DEFAULT_PLUGIN_STORE_DIR).resolve()
        self.builtin_dir = Path(builtin_dir or DEFAULT_PLUGIN_DIR).resolve()
        self.installed_dir = self.store_dir / "installed"
        self.staging_dir = self.store_dir / "staging"
        self.trash_dir = self.store_dir / "trash"
        self.registry = PluginRegistry(self.registry_path)

    def validate(self, source: str | Path) -> PackageInspection:
        """Inspect a directory or zip without changing installed state."""
        source_path = Path(source).resolve()
        if source_path.is_dir():
            package_root = self._find_package_root(source_path)
            self._validate_tree(package_root)
            return inspect_package(package_root)
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory(
                prefix=".validate-",
                dir=self._ensure_store_dir(self.staging_dir),
            ) as temp_dir:
                extracted = Path(temp_dir) / "source"
                self._extract_zip(source_path, extracted)
                package_root = self._find_package_root(extracted)
                self._validate_tree(package_root)
                return inspect_package(package_root)
        raise ValueError(f"{source_path}: plugin source must be a directory or .zip file")

    def install(self, source: str | Path) -> PluginRecord:
        """Install a trusted plugin package; newly installed packages are disabled."""
        source_path = Path(source).resolve()
        self._ensure_store_dir(self.staging_dir)
        with tempfile.TemporaryDirectory(
            prefix=".install-",
            dir=self.staging_dir,
        ) as temp_dir:
            prepared = Path(temp_dir) / "prepared"
            if source_path.is_dir():
                package_root = self._find_package_root(source_path)
                self._validate_tree(package_root)
                shutil.copytree(
                    package_root,
                    prepared,
                    ignore=shutil.ignore_patterns(*_IGNORED_PARTS),
                )
            elif source_path.is_file() and source_path.suffix.lower() == ".zip":
                extracted = Path(temp_dir) / "extracted"
                self._extract_zip(source_path, extracted)
                package_root = self._find_package_root(extracted)
                self._validate_tree(package_root)
                shutil.copytree(
                    package_root,
                    prepared,
                    ignore=shutil.ignore_patterns(*_IGNORED_PARTS),
                )
            else:
                raise ValueError(f"{source_path}: plugin source must be a directory or .zip file")

            inspection = inspect_package(prepared)
            existing = self.registry.get(inspection.name)
            if existing is not None:
                raise ValueError(f"plugin already installed: {inspection.name}")

            version_dir = _version_dir(inspection.version)
            target = self.installed_dir / inspection.name / version_dir
            if target.exists():
                raise ValueError(f"plugin version already exists: {target}")

            digest = self._digest_tree(prepared)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared, target)
            now = _now()
            record = PluginRecord(
                name=inspection.name,
                version=inspection.version,
                path=self._record_path(target),
                digest=digest,
                source=str(source_path),
                installed=True,
                enabled=False,
                installed_at=now,
                updated_at=now,
            )
            try:
                self.registry.set(record)
            except Exception:
                if target.exists():
                    shutil.rmtree(target)
                raise
            return record

    def enable(self, name: str) -> PluginRecord:
        return self._set_enabled(name, True)

    def disable(self, name: str) -> PluginRecord:
        return self._set_enabled(name, False)

    def remove(self, name: str) -> None:
        record = self._require_record(name)
        target = self._resolve_record_path(record)
        trash_target = self._ensure_store_dir(self.trash_dir) / f"{name}-{uuid4().hex}"
        moved = False
        if target.exists():
            os.replace(target, trash_target)
            moved = True
        try:
            self.registry.remove(name)
        except Exception:
            if moved and trash_target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(trash_target, target)
            raise

    def list_installed(self) -> tuple[PluginRecord, ...]:
        return self.registry.all()

    def runtime_roots(self) -> list[Path]:
        """Return built-ins plus packages enabled in the host registry."""
        roots: list[Path] = []
        if self.builtin_dir.is_dir():
            roots.append(self.builtin_dir)
        for record in self.registry.all():
            if not record.installed or not record.enabled:
                continue
            path = self._resolve_record_path(record)
            if not path.is_dir():
                raise ValueError(f"enabled plugin path does not exist: {path}")
            roots.append(path)
        return roots

    def _set_enabled(self, name: str, enabled: bool) -> PluginRecord:
        record = self._require_record(name)
        if record.enabled == enabled:
            return record
        updated = record.with_enabled(enabled, updated_at=_now())
        self.registry.set(updated)
        return updated

    def _require_record(self, name: str) -> PluginRecord:
        record = self.registry.get(name)
        if record is None:
            raise ValueError(f"plugin is not installed: {name}")
        return record

    def _resolve_record_path(self, record: PluginRecord) -> Path:
        path = Path(record.path)
        if not path.is_absolute():
            path = self.registry_path.parent / path
        resolved = path.resolve()
        try:
            resolved.relative_to(self.installed_dir)
        except ValueError as exc:
            raise ValueError(
                f"registry path for {record.name!r} is outside installed store: {resolved}"
            ) from exc
        return resolved

    def _record_path(self, target: Path) -> str:
        return Path(os.path.relpath(target, self.registry_path.parent)).as_posix()

    def _find_package_root(self, root: Path) -> Path:
        if (root / "plugin.json").is_file():
            return root
        children = [
            child for child in root.iterdir() if child.is_dir() and child.name not in _IGNORED_PARTS
        ]
        if len(children) == 1 and (children[0] / "plugin.json").is_file():
            return children[0]
        raise ValueError(
            f"{root}: package must contain plugin.json at its root or in one top-level directory"
        )

    def _validate_tree(self, root: Path) -> None:
        file_count = 0
        total_bytes = 0
        for path in root.rglob("*"):
            if _is_ignored(path, root):
                continue
            if path.is_symlink():
                raise ValueError(f"{path}: plugin packages may not contain symbolic links")
            if not path.is_file():
                continue
            file_count += 1
            total_bytes += path.stat().st_size
            if file_count > MAX_PACKAGE_FILES:
                raise ValueError(f"{root}: package contains more than {MAX_PACKAGE_FILES} files")
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ValueError(f"{root}: package exceeds the {MAX_PACKAGE_BYTES} byte size limit")

    def _extract_zip(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            archive = zipfile.ZipFile(source)
        except zipfile.BadZipFile as exc:
            raise ValueError(f"{source}: invalid zip archive: {exc}") from exc

        file_count = 0
        total_bytes = 0
        with archive:
            for member in archive.infolist():
                relative = _safe_archive_path(member.filename, source)
                target = destination / relative
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if _is_symlink(member):
                    raise ValueError(f"{source}: symbolic links are not allowed: {member.filename}")
                if member.flag_bits & 0x1:
                    raise ValueError(f"{source}: encrypted zip entries are not supported")
                file_count += 1
                if file_count > MAX_PACKAGE_FILES:
                    raise ValueError(
                        f"{source}: package contains more than {MAX_PACKAGE_FILES} files"
                    )
                if total_bytes + member.file_size > MAX_PACKAGE_BYTES:
                    raise ValueError(
                        f"{source}: package exceeds the {MAX_PACKAGE_BYTES} byte size limit"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as reader, target.open("wb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        total_bytes += len(chunk)
                        if total_bytes > MAX_PACKAGE_BYTES:
                            raise ValueError(
                                f"{source}: package exceeds the {MAX_PACKAGE_BYTES} byte size limit"
                            )
                        writer.write(chunk)

    def _digest_tree(self, root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(root.rglob("*")):
            if _is_ignored(path, root) or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _ensure_store_dir(path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path


def _version_dir(version: str) -> str:
    if not version:
        return "unversioned"
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"unsafe plugin version: {version!r}")
    return version


def _safe_archive_path(name: str, source: Path) -> Path:
    normalized = name.replace("\\", "/")
    path = Path(normalized)
    if normalized.startswith("/") or path.is_absolute() or path.drive or ".." in path.parts:
        raise ValueError(f"{source}: zip entry escapes package root: {name}")
    if not normalized or normalized in {".", "./"}:
        return Path()
    return path


def _is_symlink(member: zipfile.ZipInfo) -> bool:
    return (member.external_attr >> 16) & 0o170000 == 0o120000


def _is_ignored(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return any(part in _IGNORED_PARTS for part in relative.parts)


def _now() -> str:
    return datetime.now(UTC).isoformat()
