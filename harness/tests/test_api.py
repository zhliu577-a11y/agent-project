import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.main import create_app
from api.services import (
    ApprovalBroker,
    ConversationBusyError,
    EventJournal,
    InvalidMcpPreloadError,
    RuntimeNotReadyError,
    TurnResult,
    UnknownModelError,
)
from core.events import Event, EventIdentity
from core.session import SessionMetadata, SessionSnapshot
from core.types import Message
from plugins.manager import PluginManager


def _write_package(root: Path, name: str = "quality") -> Path:
    package = root / name
    package.mkdir(parents=True)
    (package / "plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": "1",
                "name": name,
                "version": "1.0.0",
                "contributes": [
                    {
                        "id": "lint",
                        "kind": "skill",
                        "contract": "skill.v1",
                        "entry": {"content": "SKILL.md"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (package / "SKILL.md").write_text("# lint\n", encoding="utf-8")
    return package


def _package_zip(source: Path, target: Path) -> Path:
    with zipfile.ZipFile(target, "w") as writer:
        for path in source.rglob("*"):
            if path.is_file():
                writer.write(path, path.relative_to(source.parent).as_posix())
    return target


def _manager(tmp_path: Path) -> PluginManager:
    return PluginManager(
        registry_path=tmp_path / "data" / "plugin-registry.json",
        store_dir=tmp_path / "data" / "plugin-store",
        builtin_dir=tmp_path / "builtins",
    )


class _FakeController:
    def __init__(self, manager: PluginManager) -> None:
        self.plugin_manager = manager
        self.approvals = ApprovalBroker(timeout=1)
        self.started = False
        self.busy = False
        self.active_model = "fake"
        self.mcp_preload: list[str] = []
        self.event_journal = EventJournal()
        self.active_session_id = "test"
        self.session_messages: dict[str, list[Message]] = {
            "test": [
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
            ]
        }
        self.session_metadata: dict[str, SessionMetadata] = {
            "test": SessionMetadata(
                revision=1,
                message_count=2,
                title="Existing conversation",
            )
        }

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.started,
            "started": self.started,
            "sessionId": self.active_session_id if self.started else None,
            "startedAt": None,
            "busy": self.busy,
            "error": None,
        }

    def snapshot(self) -> dict[str, Any] | None:
        if not self.started:
            return None
        return {
            "ready": True,
            "plugins": [],
            "tools": ["text__slugify"],
            "mcp": [
                {
                    "name": name,
                    "status": "idle",
                    "preload": name in self.mcp_preload,
                }
                for name in ("math", "time")
            ],
            "models": [
                {
                    "name": name,
                    "status": "active",
                    "active": name == self.active_model,
                }
                for name in ("fake", "backup")
            ],
            "skills": [],
        }

    async def start(self) -> None:
        self.started = True

    async def restart(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    def ensure_ready(self) -> None:
        if not self.started:
            raise RuntimeNotReadyError("runtime is not ready")

    async def activate_model(self, name: str) -> str:
        self.ensure_ready()
        if self.busy:
            raise ConversationBusyError(
                "cannot switch models while a conversation turn is running"
            )
        if name not in {"fake", "backup"}:
            raise UnknownModelError(f"unknown model provider {name!r}")
        self.active_model = name
        return name

    def list_model_settings(self) -> list[dict[str, Any]]:
        self.ensure_ready()
        return [self.model_settings("fake"), self.model_settings("backup")]

    def model_settings(self, name: str) -> dict[str, Any]:
        self.ensure_ready()
        if name not in {"fake", "backup"}:
            raise UnknownModelError(f"unknown model provider {name!r}")
        return {
            "name": name,
            "hasApiKey": True,
            "baseUrl": "https://example.test/v1",
            "model": "test-model",
        }

    def events(self, **filters: Any) -> list[dict[str, Any]]:
        return self.event_journal.query(**filters)

    @property
    def event_cursor(self) -> int:
        return self.event_journal.latest_seq

    def list_mcp_preload(self) -> dict[str, Any]:
        self.ensure_ready()
        return {
            "preload": list(self.mcp_preload),
            "available": ["math", "time"],
            "mcp": (self.snapshot() or {}).get("mcp", []),
        }

    async def configure_mcp_preload(
        self,
        names: list[str],
        *,
        restart: bool = True,
    ) -> dict[str, Any]:
        self.ensure_ready()
        if len(names) != len(set(names)):
            raise InvalidMcpPreloadError("duplicate MCP plugin name")
        unknown = sorted(set(names) - {"math", "time"})
        if unknown:
            raise InvalidMcpPreloadError(f"unknown MCP plugin: {unknown}")
        self.mcp_preload = list(names)
        return self.list_mcp_preload()

    async def configure_model(
        self,
        name: str,
        changes: dict[str, Any],
        *,
        activate: bool = True,
    ) -> dict[str, Any]:
        self.ensure_ready()
        if name not in {"fake", "backup"}:
            raise UnknownModelError(f"unknown model provider {name!r}")
        if activate:
            await self.activate_model(name)
        return {
            **self.model_settings(name),
            "hasApiKey": bool(changes.get("apiKey")) or self.model_settings(name)["hasApiKey"],
        }

    async def list_sessions(self) -> list[dict[str, Any]]:
        self.ensure_ready()
        return [
            {
                "id": session_id,
                "title": metadata.title,
                "createdAt": metadata.created_at,
                "updatedAt": metadata.updated_at,
                "messageCount": metadata.message_count,
                "revision": metadata.revision,
                "active": session_id == self.active_session_id,
            }
            for session_id, metadata in self.session_metadata.items()
        ]

    async def create_session(self, title: str = "") -> dict[str, Any]:
        self.ensure_ready()
        session_id = f"session-{len(self.session_metadata) + 1}"
        metadata = SessionMetadata(title=title)
        self.session_metadata[session_id] = metadata
        self.session_messages[session_id] = []
        self.active_session_id = session_id
        return next(
            item for item in await self.list_sessions() if item["id"] == session_id
        )

    async def get_session(self, session_id: str) -> SessionSnapshot:
        self.ensure_ready()
        return SessionSnapshot(
            session_id=session_id,
            messages=list(self.session_messages.get(session_id, [])),
            metadata=self.session_metadata[session_id],
        )

    async def activate_session(self, session_id: str) -> SessionSnapshot:
        self.active_session_id = session_id
        return await self.get_session(session_id)

    async def delete_session(self, session_id: str) -> str:
        self.ensure_ready()
        self.session_metadata.pop(session_id, None)
        self.session_messages.pop(session_id, None)
        if session_id == self.active_session_id:
            if self.session_metadata:
                self.active_session_id = next(iter(self.session_metadata))
            else:
                created = await self.create_session()
                self.active_session_id = str(created["id"])
        return self.active_session_id

    async def submit(
        self,
        message: str,
        *,
        session_id: str | None = None,
        max_turns: int,
        on_token=None,
    ) -> TurnResult:
        self.ensure_ready()
        if session_id is not None:
            await self.activate_session(session_id)
        if on_token is not None:
            on_token("hello")
        return TurnResult(
            session_id=session_id or self.active_session_id,
            content="hello",
            stop_reason="done",
            messages=(
                Message(role="user", content=message),
                Message(role="assistant", content="hello"),
            ),
        )

    async def stream_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        max_turns: int,
    ):
        result = await self.submit(
            message,
            session_id=session_id,
            max_turns=max_turns,
            on_token=None,
        )
        yield {"event": "token", "data": {"delta": "hello"}}
        yield {"event": "done", "data": result.to_dict()}


def test_api_plugin_inspect_install_and_lifecycle(tmp_path) -> None:
    manager = _manager(tmp_path)
    controller = _FakeController(manager)
    archive = _package_zip(
        _write_package(tmp_path / "source"),
        tmp_path / "quality.zip",
    )

    with TestClient(create_app(controller, autostart=False)) as client:
        inspected = client.post(
            "/api/v1/plugins/inspect",
            files={"package": ("quality.zip", archive.read_bytes(), "application/zip")},
        )
        assert inspected.status_code == 200
        assert inspected.json()["name"] == "quality"
        assert inspected.json()["contributions"][0]["kind"] == "skill"

        installed = client.post(
            "/api/v1/plugins/install",
            files={"package": ("quality.zip", archive.read_bytes(), "application/zip")},
        )
        assert installed.status_code == 200
        assert installed.json()["plugin"]["enabled"] is False

        enabled = client.post("/api/v1/plugins/quality/enable")
        assert enabled.status_code == 200
        assert enabled.json()["plugin"]["enabled"] is True

        listed = client.get("/api/v1/plugins")
        assert listed.status_code == 200
        assert listed.json()[0]["name"] == "quality"

        removed = client.delete("/api/v1/plugins/quality")
        assert removed.status_code == 200
        assert removed.json()["removed"] == "quality"


def test_api_chat_and_runtime_snapshot(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        started = client.post("/api/v1/runtime/start")
        assert started.status_code == 200
        assert started.json()["ready"] is True

        response = client.post(
            "/api/v1/chat",
            json={"message": "hello", "maxTurns": 3},
        )
        assert response.status_code == 200
        assert response.json()["content"] == "hello"
        assert response.json()["stopReason"] == "done"

        tools = client.get("/api/v1/tools")
        assert tools.status_code == 200
        assert tools.json()["tools"] == ["text__slugify"]


def test_api_chat_stream_and_unknown_approval(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        client.post("/api/v1/runtime/start")
        response = client.post(
            "/api/v1/chat/stream",
            json={"message": "hello"},
        )

        assert response.status_code == 200
        assert "event: token" in response.text
        assert "event: done" in response.text

        missing = client.post(
            "/api/v1/approvals/missing",
            json={"approved": True},
        )
        assert missing.status_code == 404


def test_api_manages_conversation_history(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        client.post("/api/v1/runtime/start")

        listed = client.get("/api/v1/sessions")
        assert listed.status_code == 200
        assert listed.json()["sessions"][0]["id"] == "test"

        created = client.post("/api/v1/sessions", json={"title": "Page session"})
        assert created.status_code == 201
        session_id = created.json()["id"]
        assert created.json()["title"] == "Page session"

        detail = client.get(f"/api/v1/sessions/{session_id}")
        assert detail.status_code == 200
        assert detail.json()["messages"] == []

        response = client.post(
            "/api/v1/chat",
            json={"message": "hello", "sessionId": session_id},
        )
        assert response.status_code == 200
        assert response.json()["sessionId"] == session_id

        removed = client.delete(f"/api/v1/sessions/{session_id}")
        assert removed.status_code == 200
        assert removed.json()["deleted"] == session_id
        assert removed.json()["activeSessionId"] != session_id


def test_api_activates_model_and_reports_busy(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        unavailable = client.post("/api/v1/models/backup/activate")
        assert unavailable.status_code == 503

        client.post("/api/v1/runtime/start")
        activated = client.post("/api/v1/models/backup/activate")
        assert activated.status_code == 200
        assert activated.json()["activeModel"] == "backup"
        models = {item["name"]: item for item in activated.json()["models"]}
        assert models["backup"]["active"] is True
        assert models["fake"]["active"] is False

        missing = client.post("/api/v1/models/missing/activate")
        assert missing.status_code == 404

        controller.busy = True
        busy = client.post("/api/v1/models/fake/activate")
        assert busy.status_code == 409


def test_api_reads_and_updates_model_settings_without_echoing_secret(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        client.post("/api/v1/runtime/start")
        listed = client.get("/api/v1/models/settings")
        assert listed.status_code == 200
        assert listed.json()["settings"][0]["hasApiKey"] is True
        assert "apiKey" not in listed.json()["settings"][0]

        updated = client.put(
            "/api/v1/models/fake/settings",
            json={
                "apiKey": "page-secret",
                "baseUrl": "http://127.0.0.1:9000/v1",
                "model": "page-model",
                "timeout": 30,
                "maxRetries": 2,
                "activate": True,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["settings"]["name"] == "fake"
        assert "apiKey" not in updated.json()["settings"]
        active = next(item for item in updated.json()["models"] if item["active"])
        assert active["name"] == "fake"


def test_api_rejects_non_zip_upload(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))

    with TestClient(create_app(controller, autostart=False)) as client:
        response = client.post(
            "/api/v1/plugins/inspect",
            files={"package": ("plugin.json", b"{}", "application/json")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "plugin package upload must be a .zip file"


def test_api_manages_mcp_preload_and_reads_activity(tmp_path) -> None:
    controller = _FakeController(_manager(tmp_path))
    controller.event_journal.record(
        Event(
            name="tool.after",
            payload={
                "tool_call": {"name": "math__add"},
                "apiKey": "must-not-leak",
                "ok": True,
            },
            publisher=EventIdentity.host("agent-loop"),
            session_id="test",
        )
    )

    with TestClient(create_app(controller, autostart=False)) as client:
        client.post("/api/v1/runtime/start")

        preload = client.get("/api/v1/mcp/preload")
        assert preload.status_code == 200
        assert preload.json()["preload"] == []

        updated = client.put("/api/v1/mcp/preload", json={"preload": ["math"]})
        assert updated.status_code == 200
        assert updated.json()["preload"] == ["math"]
        assert next(item for item in updated.json()["mcp"] if item["name"] == "math")[
            "preload"
        ] is True

        invalid = client.put("/api/v1/mcp/preload", json={"preload": ["missing"]})
        assert invalid.status_code == 400

        activity = client.get("/api/v1/events?after=0&name=tool.after")
        assert activity.status_code == 200
        assert activity.json()["latestSeq"] == 1
        event = activity.json()["events"][0]
        assert event["target"] == "math__add"
        assert event["status"] == "ok"
        assert event["payload"]["apiKey"] == "[REDACTED]"


def test_event_journal_restores_history_and_sequence(tmp_path) -> None:
    path = tmp_path / "data" / "event-journal.jsonl"
    journal = EventJournal(path)
    journal.record(
        Event(
            name="tool.start",
            payload={"tool_call": {"name": "text__slugify"}},
            publisher=EventIdentity.host("agent-loop"),
        )
    )

    restored = EventJournal(path)
    assert restored.latest_seq == 1
    assert restored.query(after=0, limit=10)[0]["name"] == "tool.start"

    restored.record(
        Event(
            name="tool.after",
            payload={"ok": False, "reason": "denied"},
            publisher=EventIdentity.host("agent-loop"),
        )
    )
    assert restored.latest_seq == 2


def test_event_journal_repairs_duplicate_sequences_on_reload(tmp_path) -> None:
    path = tmp_path / "data" / "event-journal.jsonl"
    first = {
        "seq": 1,
        "eventId": "one",
        "name": "runtime.started",
        "payload": {},
    }
    duplicate = {
        "seq": 1,
        "eventId": "two",
        "name": "runtime.started",
        "payload": {},
    }
    path.parent.mkdir(parents=True)
    path.write_text(
        "\n".join(json.dumps(item) for item in (first, duplicate)) + "\n",
        encoding="utf-8",
    )

    journal = EventJournal(path)
    records = journal.query(after=0, limit=10)

    assert [record["seq"] for record in records] == [1, 2]
    assert journal.latest_seq == 2
