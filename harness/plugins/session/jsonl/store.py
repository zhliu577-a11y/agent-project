"""JSONL session backend with revision checks and atomic replacement."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.session import (
    SessionMetadata,
    SessionSnapshot,
    SessionStore,
    ensure_revision,
    validate_session_id,
)
from core.types import Message, message_from_dict, message_to_dict

if os.name == "nt":  # pragma: no cover - platform branch
    import msvcrt
else:  # pragma: no cover - platform branch
    import fcntl


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Provide a cross-process lock using the host platform's file primitive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on Unix CI
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":  # pragma: no cover - platform branch
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - platform branch
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonlSessionStore(SessionStore):
    """Store messages as JSONL with separate revision and checkpoint files."""

    supports_revisioning = True

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def _session_id(self, session_id: str) -> str:
        return validate_session_id(session_id)

    def _path(self, session_id: str) -> Path:
        return self._data_dir / f"{self._session_id(session_id)}.jsonl"

    def _metadata_path(self, session_id: str) -> Path:
        return self._data_dir / f"{self._session_id(session_id)}.metadata.json"

    def _checkpoint_path(self, session_id: str) -> Path:
        return self._data_dir / f"{self._session_id(session_id)}.checkpoint.json"

    def _lock_path(self, session_id: str) -> Path:
        return self._data_dir / f".{self._session_id(session_id)}.lock"

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _load_unlocked(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        if not path.exists():
            return []
        messages: list[Message] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            messages.append(message_from_dict(json.loads(line)))
        return messages

    def _read_metadata_unlocked(self, session_id: str) -> tuple[SessionMetadata, list[Message]]:
        messages = self._load_unlocked(session_id)
        path = self._metadata_path(session_id)
        raw: dict[str, Any] | None = None
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError(f"{path}: session metadata must be a JSON object")
            raw = loaded
        metadata = SessionMetadata.from_dict(raw)
        if messages and metadata.revision == 0:
            metadata = replace(metadata, revision=1)
        if metadata.message_count != len(messages):
            metadata = replace(
                metadata,
                message_count=len(messages),
                revision=max(metadata.revision, 1 if messages else 0),
            )
            self._write_metadata_unlocked(session_id, metadata)
        return metadata, messages

    def _write_metadata_unlocked(self, session_id: str, metadata: SessionMetadata) -> None:
        _atomic_write_text(
            self._metadata_path(session_id),
            json.dumps(metadata.to_dict(), ensure_ascii=False, indent=2),
        )

    def _write_messages_unlocked(self, session_id: str, messages: list[Message]) -> None:
        payload = "\n".join(
            json.dumps(message_to_dict(message), ensure_ascii=False) for message in messages
        )
        _atomic_write_text(
            self._path(session_id),
            payload + ("\n" if payload else ""),
        )

    async def load(self, session_id: str) -> list[Message]:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                return self._load_unlocked(session_id)

    async def get(self, session_id: str, limit: int | None = None) -> list[Message]:
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 0
        ):
            raise ValueError("limit must be a non-negative integer or None")
        messages = await self.load(session_id)
        if limit is None or limit == 0:
            return messages
        return messages[-limit:]

    async def save(self, session_id: str, messages: list[Message]) -> None:
        await self.replace(session_id, messages, expected_revision=None)

    async def append(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                metadata, current = self._read_metadata_unlocked(session_id)
                ensure_revision(expected_revision, metadata.revision)
                updated = [*current, *messages]
                self._write_messages_unlocked(session_id, updated)
                next_metadata = metadata.advanced(message_count=len(updated))
                self._write_metadata_unlocked(session_id, next_metadata)
                return next_metadata

    async def replace(
        self,
        session_id: str,
        messages: list[Message],
        *,
        expected_revision: int | None = None,
    ) -> SessionMetadata:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                metadata, _ = self._read_metadata_unlocked(session_id)
                ensure_revision(expected_revision, metadata.revision)
                self._write_messages_unlocked(session_id, messages)
                next_metadata = metadata.advanced(message_count=len(messages))
                self._write_metadata_unlocked(session_id, next_metadata)
                return next_metadata

    async def clear(self, session_id: str) -> SessionMetadata:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                metadata, _ = self._read_metadata_unlocked(session_id)
                self._path(session_id).unlink(missing_ok=True)
                self._checkpoint_path(session_id).unlink(missing_ok=True)
                next_metadata = metadata.advanced(message_count=0)
                self._write_metadata_unlocked(session_id, next_metadata)
                return next_metadata

    async def load_metadata(self, session_id: str) -> SessionMetadata:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                metadata, _ = self._read_metadata_unlocked(session_id)
                return metadata

    async def save_metadata(self, session_id: str, metadata: SessionMetadata) -> None:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                self._write_metadata_unlocked(session_id, metadata)

    async def list_sessions(self) -> list[tuple[str, SessionMetadata]]:
        if not self._data_dir.is_dir():
            return []

        session_ids: set[str] = set()
        for path in self._data_dir.glob("*.metadata.json"):
            session_ids.add(path.name[: -len(".metadata.json")])
        for path in self._data_dir.glob("*.jsonl"):
            session_ids.add(path.name[: -len(".jsonl")])

        sessions: list[tuple[str, SessionMetadata]] = []
        for session_id in sorted(session_ids):
            try:
                session_id = self._session_id(session_id)
            except ValueError:
                continue
            async with self._lock(session_id):
                with _exclusive_file_lock(self._lock_path(session_id)):
                    metadata, _messages = self._read_metadata_unlocked(session_id)
            sessions.append((session_id, metadata))
        sessions.sort(
            key=lambda item: (item[1].updated_at, item[0]),
            reverse=True,
        )
        return sessions

    async def delete(self, session_id: str) -> None:
        session_id = self._session_id(session_id)
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                self._path(session_id).unlink(missing_ok=True)
                self._metadata_path(session_id).unlink(missing_ok=True)
                self._checkpoint_path(session_id).unlink(missing_ok=True)
        self._lock_path(session_id).unlink(missing_ok=True)

    async def snapshot(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> SessionSnapshot:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                metadata, messages = self._read_metadata_unlocked(session_id)
                checkpoint = self._load_checkpoint_unlocked(session_id)
                selected = messages if limit is None or limit == 0 else messages[-limit:]
                return SessionSnapshot(
                    session_id=session_id,
                    messages=selected,
                    metadata=metadata,
                    checkpoint=checkpoint,
                )

    def _load_checkpoint_unlocked(self, session_id: str) -> dict[str, Any] | None:
        path = self._checkpoint_path(session_id)
        if not path.exists():
            return None
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: checkpoint must be a JSON object")
        return loaded

    async def load_checkpoint(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                return self._load_checkpoint_unlocked(session_id)

    async def save_checkpoint(self, session_id: str, snapshot: dict[str, Any]) -> None:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                _atomic_write_text(
                    self._checkpoint_path(session_id),
                    json.dumps(snapshot, ensure_ascii=False, indent=2),
                )

    async def delete_checkpoint(self, session_id: str) -> None:
        async with self._lock(session_id):
            with _exclusive_file_lock(self._lock_path(session_id)):
                self._checkpoint_path(session_id).unlink(missing_ok=True)


def create_store(plugin_dir, context=None):
    """Create the JSONL store using SESSION_DATA_DIR or the project default."""
    default_dir = Path(plugin_dir).resolve().parents[2] / ".sessions"
    config = dict(getattr(context, "config", {}) or {})
    data_dir = Path(os.getenv("SESSION_DATA_DIR", config.get("dataDir", str(default_dir))))
    return JsonlSessionStore(data_dir)
