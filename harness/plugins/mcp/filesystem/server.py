from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

MAX_READ_BYTES = 2 * 1024 * 1024


def _default_root() -> Path:
    configured = os.getenv("HARNESS_FILESYSTEM_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    # server.py -> filesystem -> mcp -> plugins -> harness
    return (Path(__file__).resolve().parents[3] / "data" / "workspace").resolve()


def _allowed_roots() -> tuple[Path, ...]:
    raw_roots = sys.argv[1:]
    roots = (
        tuple(Path(value).expanduser().resolve() for value in raw_roots)
        if raw_roots
        else (_default_root(),)
    )
    unique_roots = tuple(dict.fromkeys(roots))
    for root in unique_roots:
        root.mkdir(parents=True, exist_ok=True)
    return unique_roots


_ROOTS = _allowed_roots()
server = MCPServer(
    name="filesystem-server",
    instructions="Read and write files only inside the configured allowed directories.",
)


def _resolve_path(raw_path: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ToolError("[invalid_path] path must be a non-empty string")
    if "\x00" in raw_path:
        raise ToolError("[invalid_path] path contains a null byte")

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = _ROOTS[0] / candidate
    resolved = candidate.resolve(strict=False)

    for root in _ROOTS:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue

    allowed = ", ".join(str(root) for root in _ROOTS)
    raise ToolError(f"[path_outside_allowed_roots] {resolved} is outside: {allowed}")


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@server.tool()
def list_allowed_directories() -> str:
    """Return the directories this filesystem server can access."""
    return "\n".join(str(root) for root in _ROOTS)


@server.tool()
def list_directory(path: str = ".", include_hidden: bool = False) -> str:
    """List files and directories inside the allowed workspace."""
    directory = _resolve_path(path)
    if not directory.exists():
        raise ToolError(f"[not_found] directory does not exist: {directory}")
    if not directory.is_dir():
        raise ToolError(f"[not_directory] path is not a directory: {directory}")

    entries: list[str] = []
    try:
        children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise ToolError(f"[read_failed] cannot list {directory}: {exc}") from exc

    for child in children:
        if not include_hidden and child.name.startswith("."):
            continue
        kind = "DIR" if child.is_dir() else "FILE"
        entries.append(f"[{kind}] {child.name}")
    return "\n".join(entries) if entries else "(empty)"


@server.tool()
def create_directory(path: str) -> str:
    """Create a directory and any missing parents inside the allowed workspace."""
    directory = _resolve_path(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ToolError(f"[create_failed] cannot create {directory}: {exc}") from exc
    return f"Created directory: {directory}"


@server.tool()
def read_text_file(path: str, encoding: str = "utf-8") -> str:
    """Read one UTF-8 text file from the allowed workspace."""
    file_path = _resolve_path(path)
    if not file_path.exists():
        raise ToolError(f"[not_found] file does not exist: {file_path}")
    if not file_path.is_file():
        raise ToolError(f"[not_file] path is not a file: {file_path}")

    try:
        size = file_path.stat().st_size
        if size > MAX_READ_BYTES:
            raise ToolError(
                f"[file_too_large] {file_path} is {size} bytes; limit is {MAX_READ_BYTES}"
            )
        return file_path.read_text(encoding=encoding)
    except ToolError:
        raise
    except (OSError, UnicodeError, LookupError) as exc:
        raise ToolError(f"[read_failed] cannot read {file_path}: {exc}") from exc


@server.tool()
def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """Write a text file, creating missing parent directories."""
    file_path = _resolve_path(path)
    if file_path.exists() and file_path.is_dir():
        raise ToolError(f"[not_file] path is a directory: {file_path}")

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding=encoding)
    except (OSError, UnicodeError, LookupError) as exc:
        raise ToolError(f"[write_failed] cannot write {file_path}: {exc}") from exc
    return f"Wrote {len(content.encode(encoding))} bytes to: {file_path}"


@server.tool()
def move_file(source: str, destination: str) -> str:
    """Move a file or directory without overwriting an existing destination."""
    source_path = _resolve_path(source)
    destination_path = _resolve_path(destination)

    if not source_path.exists():
        raise ToolError(f"[not_found] source does not exist: {source_path}")
    if source_path == _ROOTS[0] or any(source_path == root for root in _ROOTS):
        raise ToolError("[invalid_move] cannot move an allowed root")
    if destination_path.exists():
        raise ToolError(f"[already_exists] destination exists: {destination_path}")

    try:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(destination_path))
    except OSError as exc:
        raise ToolError(
            f"[move_failed] cannot move {source_path} to {destination_path}: {exc}"
        ) from exc
    return f"Moved: {source_path} -> {destination_path}"


@server.tool()
def get_file_info(path: str) -> str:
    """Return metadata for a file or directory in the allowed workspace."""
    target = _resolve_path(path)
    if not target.exists():
        raise ToolError(f"[not_found] path does not exist: {target}")

    try:
        stat = target.stat()
        kind = "directory" if target.is_dir() else "file"
        return _json(
            {
                "path": str(target),
                "name": target.name,
                "type": kind,
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    except OSError as exc:
        raise ToolError(f"[stat_failed] cannot inspect {target}: {exc}") from exc


if __name__ == "__main__":
    server.run()
