"""Host-side services used by the HTTP API.

The API does not own the agent loop or plugin lifecycle. These services keep
the HTTP layer thin by wrapping the existing Runtime, SessionGateway, event
gateway, and PluginManager contracts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv

from config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    clear_local_mcp_preload,
    load_local_mcp_preload,
    load_model_config_overlay,
    save_local_mcp_preload,
    save_model_config_overlay,
)
from core.errors import ModelError
from core.events import Event, EventIdentity, EventPublisher
from core.hooks import PromptRequest
from core.session import SessionMetadata, SessionSnapshot, validate_session_id
from core.state import capture
from core.types import Message, message_to_dict
from gateways.session_gateway import SessionGateway
from loop import run_agent
from plugins.manager import PluginManager
from runtime import HarnessRuntime, RuntimeStartupError

logger = logging.getLogger(__name__)


class RuntimeNotReadyError(RuntimeError):
    """The HTTP request needs a ready Runtime."""


class ConversationBusyError(RuntimeError):
    """The single-session Runtime is already processing a turn."""


class SessionNotFoundError(ValueError):
    """The requested conversation session does not exist."""


class InvalidSessionError(ValueError):
    """The requested conversation session id is malformed."""


class PromptRejectedError(RuntimeError):
    """A control-plane hook rejected the user's prompt."""


class UnknownModelError(ValueError):
    """The requested model provider is not part of this Runtime."""


class ModelActivationError(RuntimeError):
    """A known model provider could not be initialized."""


class InvalidModelSettingsError(ValueError):
    """The submitted model settings are malformed or unsafe."""


class ModelConfigurationError(RuntimeError):
    """A validated model settings update could not be applied."""


class InvalidMcpPreloadError(ValueError):
    """The submitted MCP preload selection is malformed or unknown."""


class McpConfigurationError(RuntimeError):
    """A validated MCP preload update could not be applied."""


_REDACTED = "[REDACTED]"
_SECRET_FIELD_MARKERS = ("key", "token", "password", "secret", "auth")


class EventJournal:
    """Bounded, redacted, process-local activity history for the workspace."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        capacity: int = 1000,
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("event journal capacity must be a positive integer")
        self.path = Path(path) if path is not None else None
        self._records: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._load_existing()

    @property
    def latest_seq(self) -> int:
        return self._seq

    def record(self, event: Event) -> None:
        self._seq += 1
        payload = _redact_value(
            {
                str(key): value
                for key, value in event.payload.items()
                if key not in {"ctx", "resp"}
            }
        )
        record = {
            "seq": self._seq,
            "eventId": event.event_id,
            "timestamp": event.ts,
            "name": event.name,
            "publisher": event.publisher.subject if event.publisher is not None else None,
            "actorKind": event.publisher.kind if event.publisher is not None else None,
            "actorName": event.publisher.name if event.publisher is not None else None,
            "sessionId": event.session_id,
            "runId": event.run_id,
            "turnId": event.turn_id,
            "traceId": event.trace_id,
            "causationId": event.causation_id,
            "correlationId": event.correlation_id,
            "status": _event_status(event.name, payload),
            "target": _event_target(payload),
            "summary": _event_summary(event.name, payload),
            "payload": payload,
        }
        self._records.append(record)
        if self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            except OSError:
                logger.exception("event journal write failed: %s", self.path)

    def _load_existing(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            logger.exception("event journal read failed: %s", self.path)
            return
        for line in lines[-self._records.maxlen :]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            seq = record.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
                continue
            normalized_seq = seq if seq > self._seq else self._seq + 1
            record["seq"] = normalized_seq
            self._records.append(record)
            self._seq = normalized_seq

    def query(
        self,
        *,
        after: int = 0,
        limit: int = 200,
        name: str | None = None,
        publisher: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_name = name.strip().lower() if name else None
        normalized_publisher = publisher.strip().lower() if publisher else None
        records = [
            record
            for record in self._records
            if record["seq"] > after
            and (
                normalized_name is None
                or normalized_name in str(record["name"]).lower()
            )
            and (
                normalized_publisher is None
                or normalized_publisher in str(record["publisher"] or "").lower()
            )
            and (session_id is None or record["sessionId"] == session_id)
            and (run_id is None or record["runId"] == run_id)
            and (trace_id is None or record["traceId"] == trace_id)
        ]
        return [dict(record) for record in records[-limit:]]


@dataclass(frozen=True)
class ApprovalInfo:
    """JSON-safe description of one pending approval request."""

    id: str
    kind: str
    name: str
    resource: str | None
    arguments: dict[str, Any] | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _PendingApproval:
    info: ApprovalInfo
    future: asyncio.Future[bool]


class ApprovalBroker:
    """Bridge host callbacks to client-side approval decisions."""

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        events: EventPublisher | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("approval timeout must be positive")
        self.timeout = float(timeout)
        self._events = events
        self._pending: dict[str, _PendingApproval] = {}
        self._closed = False

    def attach(self, events: EventPublisher) -> None:
        self._closed = False
        self._events = events

    async def close(self) -> None:
        self._closed = True
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()

    def pending(self) -> tuple[ApprovalInfo, ...]:
        return tuple(item.info for item in self._pending.values())

    async def request(
        self,
        *,
        kind: str,
        name: str,
        resource: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> bool:
        """Create a pending request and wait for one client decision."""
        if self._closed:
            return False

        approval_id = uuid4().hex
        info = ApprovalInfo(
            id=approval_id,
            kind=kind,
            name=name,
            resource=resource,
            arguments=dict(arguments or {}) or None,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = _PendingApproval(info=info, future=future)

        await self._publish_request(info)
        try:
            return await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError:
            logger.warning("approval request timed out: %s", approval_id)
            return False
        finally:
            self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, approved: bool) -> ApprovalInfo | None:
        """Resolve one pending approval request."""
        pending = self._pending.get(approval_id)
        if pending is None:
            return None
        if not pending.future.done():
            pending.future.set_result(bool(approved))
        return pending.info

    async def approve_skill(
        self,
        name: str,
        resource: str | None,
        decision: object,
    ) -> bool:
        """ApprovalCallback implementation for UseSkill."""
        reason = getattr(decision, "reason", None)
        arguments: dict[str, Any] = {}
        if reason:
            arguments["reason"] = reason
        return await self.request(
            kind="skill",
            name=name,
            resource=resource,
            arguments=arguments or None,
        )

    async def confirm_tool(self, _ctx: object, tool_call: object) -> bool:
        """ConfirmFn implementation for tool_before hooks."""
        name = str(getattr(tool_call, "name", "tool"))
        arguments = getattr(tool_call, "arguments", {})
        return await self.request(
            kind="tool",
            name=name,
            arguments=dict(arguments) if isinstance(arguments, dict) else {},
        )

    async def confirm_prompt(self, request: PromptRequest) -> bool:
        """PromptConfirmFn implementation for user_prompt_submit hooks."""
        return await self.request(
            kind="prompt",
            name="user_prompt_submit",
            arguments={"text": request.text},
        )

    async def _publish_request(self, info: ApprovalInfo) -> None:
        if self._events is None:
            return
        try:
            await self._events.publish(
                Event(
                    name="approval.requested",
                    payload=info.to_dict(),
                )
            )
        except Exception:
            logger.exception("approval request event publish failed")


@dataclass(frozen=True)
class TurnResult:
    """Result of one completed conversation turn."""

    session_id: str
    content: str
    stop_reason: str
    messages: tuple[Message, ...]
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "content": self.content,
            "stopReason": self.stop_reason,
            "messages": [message_to_dict(message) for message in self.messages],
            "error": self.error,
        }


class ConversationService:
    """Serialize turns and switch between persisted SessionGateway instances."""

    def __init__(
        self,
        runtime: HarnessRuntime,
        approvals: ApprovalBroker,
    ) -> None:
        self._runtime = runtime
        self._approvals = approvals
        self._store = runtime.session_store
        self._session = runtime.session
        if self._store is None:
            raise RuntimeNotReadyError("session store service is not initialized")
        if self._session is None:
            raise RuntimeNotReadyError("session gateway is not initialized")
        self._session_id = self._session.session_id
        self._history = list(runtime.history)
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def history(self) -> tuple[Message, ...]:
        return tuple(self._history)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_id=self._session_id,
            messages=list(self._history),
            metadata=self._session.metadata,
            checkpoint=self._session.checkpoint,
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        """List sessions with active-state decoration for the console."""
        sessions = await self._store.list_sessions()  # type: ignore[attr-defined]
        return [
            _session_summary(
                session_id,
                metadata,
                active=session_id == self._session_id,
            )
            for session_id, metadata in sessions
        ]

    async def create_session(self, title: str = "") -> dict[str, Any]:
        """Create and activate a new persisted session."""
        if self._busy:
            raise ConversationBusyError(
                "cannot create a conversation while a turn is running"
            )
        session_id = _new_session_id()
        metadata = SessionMetadata(title=_clean_session_title(title))
        await self._store.save_metadata(session_id, metadata)  # type: ignore[attr-defined]
        await self.activate_session(session_id)
        return _session_summary(
            session_id,
            self._session.metadata,
            active=True,
        )

    async def activate_session(self, session_id: str) -> SessionSnapshot:
        """Switch the active conversation without rebuilding the Runtime."""
        if self._busy:
            raise ConversationBusyError(
                "cannot switch conversations while a turn is running"
            )
        try:
            normalized = validate_session_id(session_id)
        except ValueError as exc:
            raise InvalidSessionError(str(exc)) from exc
        if normalized == self._session_id:
            return self.session

        gateway = self._create_gateway(normalized)
        snapshot = await gateway.load_snapshot()
        self._session = gateway
        self._session_id = normalized
        self._history = list(snapshot.messages)
        self._runtime.session = gateway
        self._runtime.history = list(snapshot.messages)
        self._runtime.session_checkpoint = snapshot.checkpoint
        return snapshot

    async def get_session(self, session_id: str) -> SessionSnapshot:
        """Read one persisted session without changing the active session."""
        try:
            normalized = validate_session_id(session_id)
        except ValueError as exc:
            raise InvalidSessionError(str(exc)) from exc
        known = {item[0] for item in await self._store.list_sessions()}  # type: ignore[attr-defined]
        if normalized not in known:
            raise SessionNotFoundError(f"session {normalized!r} was not found")
        return await self._store.snapshot(normalized)  # type: ignore[attr-defined]

    async def delete_session(self, session_id: str) -> str:
        """Delete one session and return the active session after deletion."""
        if self._busy:
            raise ConversationBusyError(
                "cannot delete a conversation while a turn is running"
            )
        try:
            normalized = validate_session_id(session_id)
        except ValueError as exc:
            raise InvalidSessionError(str(exc)) from exc
        known = {item[0] for item in await self._store.list_sessions()}  # type: ignore[attr-defined]
        if normalized not in known:
            raise SessionNotFoundError(f"session {normalized!r} was not found")

        deleting_active = normalized == self._session_id
        await self._store.delete(normalized)  # type: ignore[attr-defined]
        if deleting_active:
            created = await self.create_session()
            return str(created["id"])
        return self._session_id

    def _create_gateway(self, session_id: str):
        events = self._runtime.events
        return SessionGateway(
            self._store,  # type: ignore[arg-type]
            session_id=session_id,
            compaction=self._runtime.compaction_policy,
            max_tokens=self._runtime.config.context_max_tokens,
            events=(
                events.publisher(EventIdentity.host("session-gateway"))
                if events is not None
                else None
            ),
        )

    async def activate_model(self, name: str) -> str:
        """Prewarm and activate one model while excluding conversation turns."""
        if self._busy:
            raise ConversationBusyError(
                "cannot switch models while a conversation turn is running"
            )

        gateway = self._runtime.models
        if gateway is None:
            raise RuntimeNotReadyError("model gateway is not initialized")
        if name not in gateway.available():
            raise UnknownModelError(
                f"unknown model provider {name!r}; available: {list(gateway.available())}"
            )

        self._busy = True
        try:
            await gateway.use(name, prewarm=True)
        except ModelError as exc:
            raise ModelActivationError(str(exc)) from exc
        finally:
            self._busy = False
        return name

    async def submit(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        max_turns: int = 20,
        on_token: Callable[[str], None] | None = None,
    ) -> TurnResult:
        if self._busy:
            raise ConversationBusyError("a conversation turn is already running")
        if not user_input.strip():
            raise ValueError("message must not be empty")
        if max_turns <= 0:
            raise ValueError("maxTurns must be positive")
        if self._runtime.model is None:
            raise RuntimeNotReadyError("model is not initialized")

        if session_id is not None:
            await self.activate_session(session_id)
        self._busy = True
        try:
            return await self._submit_locked(
                user_input.strip(),
                max_turns=max_turns,
                on_token=on_token,
            )
        finally:
            self._busy = False

    async def _submit_locked(
        self,
        user_input: str,
        *,
        max_turns: int,
        on_token: Callable[[str], None] | None,
    ) -> TurnResult:
        runtime = self._runtime
        session = self._session
        session_id = self._session_id
        events = runtime.events
        publisher = (
            events.publisher(EventIdentity.host("api-conversation")) if events is not None else None
        )

        async def publish(name: str, **payload: object) -> None:
            if publisher is None:
                return
            await publisher.publish(
                Event(
                    name=name,
                    payload=dict(payload),
                    session_id=session_id,
                )
            )

        await publish("session.start", session_id=session_id)
        allowed = await runtime.hooks.user_prompt_submit(
            PromptRequest(text=user_input, session_id=session_id),
            confirm=self._approvals.confirm_prompt,
        )
        if not allowed:
            await publish("user_prompt.rejected", session_id=session_id)
            raise PromptRejectedError("user prompt was rejected by a host policy")
        await publish("user_prompt.accepted", session_id=session_id)

        previous_ids = {message.id for message in self._history}
        ctx = await run_agent(
            runtime.model,
            runtime.tools,
            runtime.hooks,
            runtime.system_prompt,
            user_input,
            max_turns=max_turns,
            on_token=on_token,
            history=self._history,
            confirm=self._approvals.confirm_tool,
            events=events,
            context_policy=runtime.context_gateway or runtime.context_policy,
            max_context_tokens=runtime.config.context_max_tokens,
            session_id=session_id,
        )
        history = ctx.messages[1:]
        new_messages = [
            message
            for message in history
            if message.id not in previous_ids and message.metadata.get("compaction") != "summary"
        ]
        if session is not None:
            committed = await session.commit_turn(history, capture(ctx))
            history = list(committed.messages)
            if not session.metadata.title:
                await session.set_title(_session_title_from_prompt(user_input))
            if runtime.memory_extraction is not None:
                await runtime.memory_extraction.process_turn(committed, new_messages)

        self._history = list(history)
        content = next(
            (message.content for message in reversed(ctx.messages) if message.role == "assistant"),
            "",
        )
        return TurnResult(
            session_id=session_id,
            content=content,
            stop_reason=ctx.stop_reason,
            messages=tuple(ctx.messages),
            error=ctx.state.get("last_error"),
        )


class RuntimeController:
    """Own the process Runtime and expose lifecycle-safe API operations."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        plugin_manager: PluginManager | None = None,
        approval_timeout: float | None = None,
    ) -> None:
        self.config_path = Path(config_path or DEFAULT_CONFIG_PATH)
        self.plugin_manager = plugin_manager or PluginManager()
        configured_timeout = approval_timeout
        if configured_timeout is None:
            configured_timeout = float(os.getenv("APPROVAL_TIMEOUT", "300"))
        self.approvals = ApprovalBroker(timeout=configured_timeout)
        self._runtime: HarnessRuntime | None = None
        self._conversation: ConversationService | None = None
        self._started_at: str | None = None
        self._last_error: str | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._model_config_lock = asyncio.Lock()
        self._mcp_config_lock = asyncio.Lock()
        config_dir = (
            self.config_path
            if self.config_path.is_dir()
            else self.config_path.parent
        )
        self._event_journal = EventJournal(config_dir.parent / "data" / "event-journal.jsonl")
        self._journal_unsubscribe: Callable[[], None] | None = None

    @property
    def runtime(self) -> HarnessRuntime | None:
        return self._runtime

    def status(self) -> dict[str, Any]:
        runtime = self._runtime
        conversation = self._conversation
        return {
            "ready": bool(runtime is not None and runtime.ready),
            "started": runtime is not None,
            "sessionId": (
                conversation.session_id
                if conversation is not None
                else runtime.session.session_id
                if runtime is not None and runtime.session is not None
                else None
            ),
            "startedAt": self._started_at,
            "busy": bool(conversation is not None and conversation.busy),
            "error": self._last_error,
        }

    def snapshot(self) -> dict[str, Any] | None:
        if self._runtime is None:
            return None
        return _snapshot_to_dict(self._runtime.snapshot())

    def events(
        self,
        *,
        after: int = 0,
        limit: int = 200,
        name: str | None = None,
        publisher: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._event_journal.query(
            after=after,
            limit=limit,
            name=name,
            publisher=publisher,
            session_id=session_id,
            run_id=run_id,
            trace_id=trace_id,
        )

    @property
    def event_cursor(self) -> int:
        return self._event_journal.latest_seq

    def list_mcp_preload(self) -> dict[str, Any]:
        runtime = self.ensure_ready()
        gateway = runtime.mcp_gateway
        if gateway is None:
            raise RuntimeNotReadyError("MCP gateway is not initialized")
        return {
            "preload": list(runtime.config.mcp_preload),
            "available": gateway.available(),
            "mcp": (_snapshot_to_dict(runtime.snapshot()) or {}).get("mcp", []),
        }

    async def configure_mcp_preload(
        self,
        names: list[str],
        *,
        restart: bool = True,
    ) -> dict[str, Any]:
        """Persist page-selected MCP startup plugins and optionally restart."""
        runtime = self.ensure_ready()
        gateway = runtime.mcp_gateway
        if gateway is None:
            raise RuntimeNotReadyError("MCP gateway is not initialized")
        if self._conversation is not None and self._conversation.busy:
            raise ConversationBusyError(
                "cannot change MCP preload while a conversation turn is running"
            )

        cleaned = _clean_mcp_preload(names, gateway.available())
        async with self._mcp_config_lock:
            if self._conversation is not None and self._conversation.busy:
                raise ConversationBusyError(
                    "cannot change MCP preload while a conversation turn is running"
                )

            config_dir = runtime.config.config_dir or self.config_path.parent
            previous_overlay = load_local_mcp_preload(config_dir)
            saved = False
            try:
                save_local_mcp_preload(config_dir, cleaned)
                saved = True
                if restart:
                    await self.restart()
            except Exception as exc:
                if saved:
                    try:
                        if previous_overlay is None:
                            clear_local_mcp_preload(config_dir)
                        else:
                            save_local_mcp_preload(config_dir, previous_overlay)
                        await self.restart()
                    except Exception:
                        logger.exception("MCP preload config rollback failed")
                if isinstance(exc, (ConversationBusyError, RuntimeNotReadyError)):
                    raise
                raise McpConfigurationError(str(exc)) from exc

            current = self.ensure_ready()
            await _publish_control_event(
                current,
                "mcp.preload_updated",
                {
                    "preload": list(cleaned),
                    "previous": list(previous_overlay or ()),
                    "restarted": restart,
                },
            )
            return self.list_mcp_preload()

    def list_model_settings(self) -> list[dict[str, Any]]:
        runtime = self.ensure_ready()
        gateway = runtime.models
        if gateway is None:
            raise RuntimeNotReadyError("model gateway is not initialized")
        return [self.model_settings(name) for name in gateway.available()]

    def model_settings(self, name: str) -> dict[str, Any]:
        runtime = self.ensure_ready()
        gateway = runtime.models
        if gateway is None:
            raise RuntimeNotReadyError("model gateway is not initialized")
        if name not in gateway.available():
            raise UnknownModelError(
                f"unknown model provider {name!r}; available: {list(gateway.available())}"
            )

        effective = _model_environment_defaults(name)
        effective.update(runtime.config.plugin_config("model", name))
        api_key = effective.pop("apiKey", None)
        settings: dict[str, Any] = {
            "name": name,
            "hasApiKey": bool(api_key),
        }
        for field in ("baseUrl", "model", "timeout", "maxRetries"):
            value = effective.get(field)
            if value is not None:
                settings[field] = value
        if name == "sub2api" or "disableResponseStorage" in effective:
            settings["disableResponseStorage"] = _coerce_bool(
                effective.get("disableResponseStorage", True)
            )
        return settings

    async def configure_model(
        self,
        name: str,
        changes: dict[str, Any],
        *,
        activate: bool = True,
    ) -> dict[str, Any]:
        """Persist local model settings, then rebuild and optionally activate the model."""
        runtime = self.ensure_ready()
        gateway = runtime.models
        if gateway is None:
            raise RuntimeNotReadyError("model gateway is not initialized")
        if name not in gateway.available():
            raise UnknownModelError(
                f"unknown model provider {name!r}; available: {list(gateway.available())}"
            )
        if self._conversation is not None and self._conversation.busy:
            raise ConversationBusyError(
                "cannot change model settings while a conversation turn is running"
            )

        cleaned = _clean_model_changes(changes)
        if not cleaned:
            if activate:
                await self.activate_model(name)
            return self.model_settings(name)

        async with self._model_config_lock:
            if self._conversation is not None and self._conversation.busy:
                raise ConversationBusyError(
                    "cannot change model settings while a conversation turn is running"
                )
            config_dir = runtime.config.config_dir or self.config_path.parent
            previous = load_model_config_overlay(config_dir, name)
            updated = dict(previous)
            for field, value in cleaned.items():
                if value in (None, ""):
                    updated.pop(field, None)
                else:
                    updated[field] = value

            if updated == previous:
                if activate:
                    await self.activate_model(name)
                return self.model_settings(name)

            saved = False
            try:
                save_model_config_overlay(config_dir, name, updated)
                saved = True
                await self.restart()
                if activate:
                    await self.activate_model(name)
            except Exception as exc:
                if saved:
                    try:
                        save_model_config_overlay(config_dir, name, previous)
                        await self.restart()
                    except Exception:
                        logger.exception("model config rollback failed: %s", name)
                raise ModelConfigurationError(str(exc)) from exc
            return self.model_settings(name)

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._runtime is not None and self._runtime.ready:
                return
            self._last_error = None
            load_dotenv()
            config = AppConfig.load(self.config_path)
            os.environ.setdefault("EMBEDDING_PROVIDER", config.embedding_provider)
            runtime = HarnessRuntime(
                config,
                plugin_manager=self.plugin_manager,
                skill_approver=self.approvals.approve_skill,
            )
            try:
                await runtime.start()
            except Exception as exc:
                await runtime.close()
                self._last_error = str(exc)
                await self.approvals.close()
                raise RuntimeStartupError(self._last_error) from exc

            self._runtime = runtime
            self._conversation = ConversationService(runtime, self.approvals)
            self.approvals.attach(runtime.events.publisher(EventIdentity.host("approval-broker")))
            self._attach_event_journal(runtime)
            self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
            await _publish_control_event(
                runtime,
                "runtime.started",
                {
                    "sessionId": runtime.config.session_id,
                    "plugins": len(runtime.snapshot().plugins),
                },
            )
            logger.info("HTTP Runtime started: session=%s", runtime.config.session_id)

    async def restart(self) -> None:
        await self.close()
        await self.start()

    async def close(self) -> None:
        async with self._lifecycle_lock:
            runtime = self._runtime
            self._runtime = None
            self._conversation = None
            self._started_at = None
            if runtime is not None and runtime.events is not None:
                await _publish_control_event(
                    runtime,
                    "runtime.stopping",
                    {"sessionId": runtime.config.session_id},
                )
                try:
                    await runtime.events.flush()
                except Exception:
                    logger.exception("event flush failed before runtime shutdown")
            if self._journal_unsubscribe is not None:
                try:
                    self._journal_unsubscribe()
                except Exception:
                    logger.exception("event journal unsubscribe failed")
                self._journal_unsubscribe = None
            if runtime is not None:
                await runtime.close()
            await self.approvals.close()

    def _attach_event_journal(self, runtime: HarnessRuntime) -> None:
        if runtime.events is None:
            return
        if self._journal_unsubscribe is not None:
            try:
                self._journal_unsubscribe()
            except Exception:
                logger.exception("event journal unsubscribe failed")
        self._journal_unsubscribe = runtime.events.subscriber(
            EventIdentity.host("event-journal")
        ).subscribe("*", self._event_journal.record)

    def ensure_ready(self) -> HarnessRuntime:
        runtime = self._runtime
        if runtime is None or not runtime.ready:
            detail = self._last_error or "runtime is not ready"
            raise RuntimeNotReadyError(detail)
        return runtime

    async def submit(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        max_turns: int = 20,
        on_token: Callable[[str], None] | None = None,
    ) -> TurnResult:
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.submit(
            user_input,
            session_id=session_id,
            max_turns=max_turns,
            on_token=on_token,
        )

    async def list_sessions(self) -> list[dict[str, Any]]:
        """Return persisted conversations without exposing store internals."""
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.list_sessions()

    async def create_session(self, title: str = "") -> dict[str, Any]:
        """Create and activate a conversation."""
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.create_session(title)

    async def get_session(self, session_id: str) -> SessionSnapshot:
        """Read one conversation and its messages."""
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.get_session(session_id)

    async def activate_session(self, session_id: str) -> SessionSnapshot:
        """Make one persisted conversation active for the next turn."""
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.activate_session(session_id)

    async def delete_session(self, session_id: str) -> str:
        """Delete one conversation and return the active session id."""
        self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        return await self._conversation.delete_session(session_id)

    async def activate_model(self, name: str) -> str:
        """Switch the active model through the serialized conversation service."""
        runtime = self.ensure_ready()
        if self._conversation is None:
            raise RuntimeNotReadyError("conversation service is not initialized")
        activated = await self._conversation.activate_model(name)
        if runtime.events is not None:
            await runtime.events.publisher(EventIdentity.host("api-model-control")).publish(
                Event(
                    name="model.activated",
                    payload={"name": activated},
                    session_id=(
                        runtime.session.session_id
                        if runtime.session is not None
                        else runtime.config.session_id
                    ),
                )
            )
        return activated

    async def stream_turn(
        self,
        user_input: str,
        *,
        session_id: str | None = None,
        max_turns: int = 20,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield token, runtime-event, approval, and final result records."""
        runtime = self.ensure_ready()
        if session_id is not None:
            await self.activate_session(session_id)
        events = runtime.events
        if events is None:
            raise RuntimeNotReadyError("event gateway is not initialized")

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def enqueue_token(text: str) -> None:
            queue.put_nowait({"event": "token", "data": {"delta": text}})

        def on_event(event: Event) -> None:
            event_name = "approval" if event.name == "approval.requested" else "runtime"
            queue.put_nowait(
                {
                    "event": event_name,
                    "data": {
                        "name": event.name,
                        "eventId": event.event_id,
                        "sessionId": event.session_id,
                        "runId": event.run_id,
                        "turnId": event.turn_id,
                        "timestamp": event.ts,
                        "payload": _safe_value(event.payload),
                    },
                }
            )

        subscriber = events.subscriber(EventIdentity.host("api-chat-stream"))
        unsubscribe = subscriber.subscribe("*", on_event)

        async def run_turn() -> None:
            result: TurnResult | None = None
            error: Exception | None = None
            try:
                result = await self.submit(
                    user_input,
                    session_id=session_id,
                    max_turns=max_turns,
                    on_token=enqueue_token,
                )
            except Exception as exc:
                error = exc
            finally:
                try:
                    await events.flush()
                except Exception:
                    logger.exception("event flush failed after API turn")
                queue.put_nowait({"event": "_done", "result": result, "error": error})

        task = asyncio.create_task(run_turn(), name="harness-api-turn")
        try:
            while True:
                item = await queue.get()
                if item["event"] == "_done":
                    result = item["result"]
                    error = item["error"]
                    if error is not None:
                        yield {
                            "event": "error",
                            "data": {
                                "type": type(error).__name__,
                                "message": str(error),
                            },
                        }
                    elif result is not None:
                        yield {"event": "done", "data": result.to_dict()}
                    return
                yield item
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            unsubscribe()
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _snapshot_to_dict(snapshot: object) -> dict[str, Any]:
    return {
        "ready": bool(getattr(snapshot, "ready", False)),
        "plugins": [asdict(item) for item in getattr(snapshot, "plugins", ())],
        "tools": list(getattr(snapshot, "tools", ())),
        "mcp": [asdict(item) for item in getattr(snapshot, "mcp", ())],
        "models": [asdict(item) for item in getattr(snapshot, "models", ())],
        "skills": [asdict(item) for item in getattr(snapshot, "skills", ())],
    }


def _new_session_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"session-{timestamp}-{uuid4().hex[:8]}"


def _clean_session_title(title: str) -> str:
    normalized = " ".join(str(title or "").split())
    if len(normalized) > 200:
        return normalized[:197].rstrip() + "..."
    return normalized


def _session_title_from_prompt(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    if len(normalized) > 72:
        return normalized[:69].rstrip() + "..."
    return normalized or "New conversation"


def _session_summary(
    session_id: str,
    metadata: SessionMetadata,
    *,
    active: bool,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "title": metadata.title,
        "createdAt": metadata.created_at,
        "updatedAt": metadata.updated_at,
        "messageCount": metadata.message_count,
        "revision": metadata.revision,
        "active": active,
    }


async def _publish_control_event(
    runtime: HarnessRuntime,
    name: str,
    payload: dict[str, Any],
) -> None:
    if runtime.events is None:
        return
    try:
        session_id = (
            runtime.session.session_id
            if runtime.session is not None
            else runtime.config.session_id
        )
        await runtime.events.publisher(EventIdentity.host("api-control")).publish(
            Event(name=name, payload=payload, session_id=session_id)
        )
    except Exception:
        logger.exception("control event publish failed: %s", name)


def _clean_mcp_preload(names: list[str], available: list[str]) -> tuple[str, ...]:
    if not isinstance(names, list):
        raise InvalidMcpPreloadError("preload must be an array of MCP plugin names")
    cleaned: list[str] = []
    seen: set[str] = set()
    available_names = set(available)
    for index, value in enumerate(names, start=1):
        if not isinstance(value, str):
            raise InvalidMcpPreloadError(f"preload[{index}] must be a string")
        name = value.strip()
        if not name:
            raise InvalidMcpPreloadError(f"preload[{index}] must not be empty")
        if name in seen:
            raise InvalidMcpPreloadError(f"duplicate MCP plugin name: {name}")
        if name not in available_names:
            raise InvalidMcpPreloadError(
                f"unknown MCP plugin {name!r}; available: {sorted(available_names)}"
            )
        seen.add(name)
        cleaned.append(name)
    return tuple(cleaned)


def _redact_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED
                if _is_secret_field(str(key))
                else _redact_value(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(item, depth=depth + 1) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _redact_value(asdict(value), depth=depth + 1)
    text = repr(value)
    return text if len(text) <= 4000 else f"{text[:3997]}..."


def _is_secret_field(name: str) -> bool:
    normalized = name.replace("-", "_").lower()
    return any(marker in normalized for marker in _SECRET_FIELD_MARKERS)


def _event_target(payload: dict[str, Any]) -> str | None:
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        if isinstance(name, str) and name:
            return name
    for field in ("target", "name", "model", "plugin", "skill", "note_id", "id"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _event_status(name: str, payload: dict[str, Any]) -> str:
    ok = payload.get("ok")
    if isinstance(ok, bool):
        return "ok" if ok else "failed"
    normalized = name.lower()
    if any(marker in normalized for marker in ("error", "failed", "denied", "rejected")):
        return "failed"
    if normalized.endswith(".start") or normalized.endswith(".request"):
        return "started"
    return "ok"


def _event_summary(name: str, payload: dict[str, Any]) -> str:
    for field in ("summary", "message", "reason", "result"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value[:300]
    target = _event_target(payload)
    return f"{name}: {target}" if target else name


_MODEL_SETTING_FIELDS = frozenset(
    {
        "apiKey",
        "baseUrl",
        "model",
        "timeout",
        "maxRetries",
        "disableResponseStorage",
    }
)


def _clean_model_changes(changes: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(changes, dict):
        raise InvalidModelSettingsError("model settings must be an object")
    unknown = sorted(set(changes) - _MODEL_SETTING_FIELDS)
    if unknown:
        raise InvalidModelSettingsError(f"unsupported model settings: {unknown}")

    cleaned: dict[str, Any] = {}
    for field, value in changes.items():
        if value is None or value == "":
            cleaned[field] = None
            continue
        if field == "apiKey":
            if not isinstance(value, str):
                raise InvalidModelSettingsError("apiKey must be a string")
            secret = value.strip()
            if not secret or len(secret) > 10_000:
                raise InvalidModelSettingsError("apiKey must be between 1 and 10000 characters")
            if any(character in secret for character in "\r\n"):
                raise InvalidModelSettingsError("apiKey must not contain line breaks")
            cleaned[field] = secret
        elif field == "baseUrl":
            if not isinstance(value, str):
                raise InvalidModelSettingsError("baseUrl must be a string")
            base_url = value.strip().rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise InvalidModelSettingsError("baseUrl must be an absolute http(s) URL")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise InvalidModelSettingsError(
                    "baseUrl must not contain credentials, a query string, or a fragment"
                )
            if len(base_url) > 2048:
                raise InvalidModelSettingsError("baseUrl must not exceed 2048 characters")
            cleaned[field] = base_url
        elif field == "model":
            if not isinstance(value, str):
                raise InvalidModelSettingsError("model must be a string")
            model = value.strip()
            if not model or len(model) > 200 or "\n" in model or "\r" in model:
                raise InvalidModelSettingsError("model must be a non-empty single-line string")
            cleaned[field] = model
        elif field == "timeout":
            if isinstance(value, bool):
                raise InvalidModelSettingsError("timeout must be a number")
            try:
                timeout = float(value)
            except (TypeError, ValueError) as exc:
                raise InvalidModelSettingsError("timeout must be a number") from exc
            if not 1 <= timeout <= 600:
                raise InvalidModelSettingsError("timeout must be between 1 and 600 seconds")
            cleaned[field] = timeout
        elif field == "maxRetries":
            if isinstance(value, bool):
                raise InvalidModelSettingsError("maxRetries must be an integer")
            try:
                retries = int(value)
            except (TypeError, ValueError) as exc:
                raise InvalidModelSettingsError("maxRetries must be an integer") from exc
            if not 0 <= retries <= 20:
                raise InvalidModelSettingsError("maxRetries must be between 0 and 20")
            cleaned[field] = retries
        elif field == "disableResponseStorage":
            cleaned[field] = _coerce_bool(value)
    return cleaned


def _model_environment_defaults(name: str) -> dict[str, Any]:
    prefix = name.upper().replace("-", "_")
    values: dict[str, Any] = {}
    for field, suffix in (
        ("apiKey", "API_KEY"),
        ("baseUrl", "BASE_URL"),
        ("model", "MODEL"),
    ):
        value = os.getenv(f"{prefix}_{suffix}")
        if value:
            values[field] = value
    timeout = os.getenv(f"{prefix}_TIMEOUT")
    if timeout:
        try:
            values["timeout"] = float(timeout)
        except ValueError:
            pass
    retries = os.getenv(f"{prefix}_MAX_RETRIES")
    if retries:
        try:
            values["maxRetries"] = int(retries)
        except ValueError:
            pass
    response_storage = os.getenv(f"{prefix}_DISABLE_RESPONSE_STORAGE")
    if response_storage:
        values["disableResponseStorage"] = response_storage
    return values


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise InvalidModelSettingsError(f"invalid boolean value: {value!r}")


def _safe_value(value: Any, *, depth: int = 0, limit: int = 1000) -> Any:
    if depth > 5:
        return "<truncated>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {
            str(key): (
                _REDACTED
                if _is_secret_field(str(key))
                else _safe_value(item, depth=depth + 1, limit=limit)
            )
            for key, item in value.items()
            if key not in {"ctx", "resp"}
        }
    if isinstance(value, (list, tuple, set)):
        return [_safe_value(item, depth=depth + 1, limit=limit) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return _safe_value(asdict(value), depth=depth + 1, limit=limit)
    return repr(value)[:limit]
