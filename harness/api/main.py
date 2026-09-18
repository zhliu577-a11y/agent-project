"""FastAPI application exposing the Harness control and conversation API."""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.services import (
    ConversationBusyError,
    InvalidMcpPreloadError,
    InvalidModelSettingsError,
    InvalidSessionError,
    McpConfigurationError,
    ModelActivationError,
    ModelConfigurationError,
    PromptRejectedError,
    RuntimeController,
    RuntimeNotReadyError,
    SessionNotFoundError,
    UnknownModelError,
)
from core.types import message_to_dict
from plugins.loader import PackageInspection
from plugins.registry import PluginRecord

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    runtime: dict[str, Any]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    maxTurns: int = Field(default=20, ge=1, le=100)
    sessionId: str | None = Field(default=None, min_length=1, max_length=128)


class SessionCreateRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class ChatResponse(BaseModel):
    sessionId: str
    content: str
    stopReason: str
    messages: list[dict[str, Any]]
    error: dict[str, Any] | None = None


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class ModelSettingsRequest(BaseModel):
    apiKey: str | None = Field(default=None, max_length=10_000)
    baseUrl: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    timeout: float | None = Field(default=None, ge=1, le=600)
    maxRetries: int | None = Field(default=None, ge=0, le=20)
    disableResponseStorage: bool | None = None
    clearApiKey: bool = False
    activate: bool = True


class McpPreloadRequest(BaseModel):
    preload: list[str] = Field(default_factory=list, max_length=100)
    restart: bool = True


def create_app(
    controller: RuntimeController | None = None,
    *,
    autostart: bool = True,
) -> FastAPI:
    """Build the API app around one process-local Runtime controller."""
    resolved_controller = controller or RuntimeController()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if autostart:
            try:
                await resolved_controller.start()
            except Exception:
                logger.exception("Runtime autostart failed; API remains available in degraded mode")
        try:
            yield
        finally:
            await resolved_controller.close()

    app = FastAPI(
        title="Harness API",
        version="1.0.0",
        description="Control plugins and run the Harness agent.",
        lifespan=lifespan,
    )
    app.state.controller = resolved_controller
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        controller = _controller(request)
        runtime = controller.status()
        return HealthResponse(
            status="ok" if runtime["ready"] else "degraded",
            runtime=runtime,
        )

    @app.get("/api/v1/runtime")
    async def runtime_status(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        return {
            **controller.status(),
            "snapshot": controller.snapshot(),
        }

    @app.post("/api/v1/runtime/start")
    async def runtime_start(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        try:
            await controller.start()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {
            **controller.status(),
            "snapshot": controller.snapshot(),
        }

    @app.post("/api/v1/runtime/restart")
    async def runtime_restart(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        try:
            await controller.restart()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {
            **controller.status(),
            "snapshot": controller.snapshot(),
        }

    @app.get("/api/v1/plugins")
    async def plugins_list(request: Request) -> list[dict[str, Any]]:
        controller = _controller(request)
        snapshot = controller.snapshot() or {}
        states = {
            item.get("name"): item for item in snapshot.get("plugins", []) if isinstance(item, dict)
        }
        return [
            _plugin_view(record, states.get(record.name))
            for record in controller.plugin_manager.list_installed()
        ]

    @app.post("/api/v1/plugins/inspect")
    async def plugins_inspect(
        request: Request,
        package: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        controller = _controller(request)
        try:
            async with _uploaded_package(controller, package) as source:
                inspection = controller.plugin_manager.validate(source)
        except (OSError, ValueError) as exc:
            raise _plugin_http_error(exc) from exc
        return _inspection_view(inspection)

    @app.post("/api/v1/plugins/install")
    async def plugins_install(
        request: Request,
        package: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        controller = _controller(request)
        try:
            async with _uploaded_package(controller, package) as source:
                record = controller.plugin_manager.install(source)
        except (OSError, ValueError) as exc:
            raise _plugin_http_error(exc, install=True) from exc
        return {
            "plugin": _plugin_view(record, None),
            "restartRequired": bool(controller.status()["started"]),
        }

    @app.post("/api/v1/plugins/{name}/enable")
    async def plugins_enable(request: Request, name: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            record = controller.plugin_manager.enable(name)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "plugin": _plugin_view(record, None),
            "restartRequired": bool(controller.status()["started"]),
        }

    @app.post("/api/v1/plugins/{name}/disable")
    async def plugins_disable(request: Request, name: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            record = controller.plugin_manager.disable(name)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "plugin": _plugin_view(record, None),
            "restartRequired": bool(controller.status()["started"]),
        }

    @app.delete("/api/v1/plugins/{name}")
    async def plugins_remove(request: Request, name: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            controller.plugin_manager.remove(name)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "removed": name,
            "restartRequired": bool(controller.status()["started"]),
        }

    @app.get("/api/v1/tools")
    async def tools_list(request: Request) -> dict[str, Any]:
        return {"tools": (_controller(request).snapshot() or {}).get("tools", [])}

    @app.get("/api/v1/mcp")
    async def mcp_list(request: Request) -> dict[str, Any]:
        return {"mcp": (_controller(request).snapshot() or {}).get("mcp", [])}

    @app.get("/api/v1/mcp/preload")
    async def mcp_preload_get(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        try:
            return controller.list_mcp_preload()
        except RuntimeNotReadyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.put("/api/v1/mcp/preload")
    async def mcp_preload_update(
        request: Request,
        body: McpPreloadRequest,
    ) -> dict[str, Any]:
        controller = _controller(request)
        try:
            return await controller.configure_mcp_preload(
                body.preload,
                restart=body.restart,
            )
        except InvalidMcpPreloadError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except ConversationBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (McpConfigurationError, RuntimeNotReadyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.get("/api/v1/events")
    async def events_list(
        request: Request,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=1000),
        name: str | None = Query(default=None, max_length=200),
        publisher: str | None = Query(default=None, max_length=200),
        sessionId: str | None = Query(default=None, max_length=200),
        runId: str | None = Query(default=None, max_length=200),
        traceId: str | None = Query(default=None, max_length=200),
    ) -> dict[str, Any]:
        controller = _controller(request)
        return {
            "events": controller.events(
                after=after,
                limit=limit,
                name=name,
                publisher=publisher,
                session_id=sessionId,
                run_id=runId,
                trace_id=traceId,
            ),
            "latestSeq": controller.event_cursor,
        }

    @app.get("/api/v1/models")
    async def models_list(request: Request) -> dict[str, Any]:
        return {"models": (_controller(request).snapshot() or {}).get("models", [])}

    @app.get("/api/v1/models/settings")
    async def model_settings_list(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        try:
            return {"settings": controller.list_model_settings()}
        except RuntimeNotReadyError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc

    @app.put("/api/v1/models/{name}/settings")
    async def model_settings_update(
        request: Request,
        name: str,
        body: ModelSettingsRequest,
    ) -> dict[str, Any]:
        controller = _controller(request)
        fields = body.model_fields_set
        changes: dict[str, Any] = {}
        for field in ("baseUrl", "model", "timeout", "maxRetries", "disableResponseStorage"):
            if field in fields:
                changes[field] = getattr(body, field)
        if body.clearApiKey:
            changes["apiKey"] = None
        elif "apiKey" in fields and body.apiKey and body.apiKey.strip():
            changes["apiKey"] = body.apiKey

        try:
            settings = await controller.configure_model(
                name,
                changes,
                activate=body.activate,
            )
        except UnknownModelError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ConversationBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except InvalidModelSettingsError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        except (ModelConfigurationError, RuntimeNotReadyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {
            "settings": settings,
            "models": (controller.snapshot() or {}).get("models", []),
        }

    @app.post("/api/v1/models/{name}/activate")
    async def models_activate(request: Request, name: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            activated = await controller.activate_model(name)
        except UnknownModelError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        except ConversationBusyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except (ModelActivationError, RuntimeNotReadyError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc),
            ) from exc
        return {
            "activeModel": activated,
            "models": (controller.snapshot() or {}).get("models", []),
        }

    @app.get("/api/v1/skills")
    async def skills_list(request: Request) -> dict[str, Any]:
        return {"skills": (_controller(request).snapshot() or {}).get("skills", [])}

    @app.get("/api/v1/sessions")
    async def sessions_list(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        try:
            return {"sessions": await controller.list_sessions()}
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/v1/sessions", status_code=status.HTTP_201_CREATED)
    async def sessions_create(
        request: Request,
        body: SessionCreateRequest,
    ) -> dict[str, Any]:
        controller = _controller(request)
        try:
            return await controller.create_session(body.title)
        except ConversationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/v1/sessions/{session_id}")
    async def sessions_get(request: Request, session_id: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            snapshot = await controller.get_session(session_id)
        except InvalidSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "id": snapshot.session_id,
            "title": snapshot.metadata.title,
            "createdAt": snapshot.metadata.created_at,
            "updatedAt": snapshot.metadata.updated_at,
            "messageCount": snapshot.metadata.message_count,
            "revision": snapshot.metadata.revision,
            "active": False,
            "messages": [message_to_dict(message) for message in snapshot.messages],
        }

    @app.delete("/api/v1/sessions/{session_id}")
    async def sessions_delete(request: Request, session_id: str) -> dict[str, Any]:
        controller = _controller(request)
        try:
            active_session_id = await controller.delete_session(session_id)
        except InvalidSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SessionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConversationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {
            "deleted": session_id,
            "activeSessionId": active_session_id,
        }

    @app.get("/api/v1/approvals")
    async def approvals_list(request: Request) -> dict[str, Any]:
        controller = _controller(request)
        return {"approvals": [approval.to_dict() for approval in controller.approvals.pending()]}

    @app.post("/api/v1/approvals/{approval_id}")
    async def approvals_resolve(
        request: Request,
        approval_id: str,
        decision: ApprovalDecisionRequest,
    ) -> dict[str, Any]:
        controller = _controller(request)
        approval = controller.approvals.resolve(approval_id, decision.approved)
        if approval is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="approval request not found or already resolved",
            )
        return {
            "approval": approval.to_dict(),
            "approved": decision.approved,
            "reason": decision.reason,
        }

    @app.post("/api/v1/chat", response_model=ChatResponse)
    async def chat(request: Request, body: ChatRequest) -> ChatResponse:
        controller = _controller(request)
        try:
            result = await controller.submit(
                body.message,
                max_turns=body.maxTurns,
            )
        except ConversationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PromptRejectedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except InvalidSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return ChatResponse(**result.to_dict())

    @app.post("/api/v1/chat/stream")
    async def chat_stream(request: Request, body: ChatRequest) -> StreamingResponse:
        controller = _controller(request)
        try:
            controller.ensure_ready()
            if body.sessionId is not None:
                await controller.activate_session(body.sessionId)
        except InvalidSessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ConversationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeNotReadyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        async def stream() -> AsyncIterator[str]:
            async for item in controller.stream_turn(
                body.message,
                max_turns=body.maxTurns,
                session_id=body.sessionId,
            ):
                yield _sse(item["event"], item["data"])

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )


def _controller(request: Request) -> Any:
    return request.app.state.controller


@asynccontextmanager
async def _uploaded_package(
    controller: RuntimeController,
    upload: UploadFile,
) -> AsyncIterator[Path]:
    filename = upload.filename or ""
    if Path(filename).suffix.lower() != ".zip":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="plugin package upload must be a .zip file",
        )

    root = controller.plugin_manager.staging_dir
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".api-upload-", dir=root) as temp_dir:
            target = Path(temp_dir) / "package.zip"
            total = 0
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail=(
                                f"plugin package exceeds the {MAX_UPLOAD_BYTES} byte upload limit"
                            ),
                        )
                    handle.write(chunk)
            yield target
    finally:
        await upload.close()


def _plugin_view(
    record: PluginRecord,
    runtime_state: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "name": record.name,
        **record.to_dict(),
        "contributions": list((runtime_state or {}).get("contributions", [])),
        "runtimeError": (runtime_state or {}).get("error"),
    }


def _inspection_view(inspection: PackageInspection) -> dict[str, Any]:
    return {
        "name": inspection.name,
        "version": inspection.version,
        "description": inspection.description,
        "contributions": [
            {
                "kind": contribution.kind,
                "id": contribution.contribution_id,
                "name": contribution.manifest.name,
                "version": contribution.manifest.version,
                "description": contribution.manifest.description,
                "protocolVersion": contribution.manifest.protocol_version,
                "contract": contribution.manifest.contract,
                "requires": [
                    {
                        "kind": requirement.kind,
                        "name": requirement.name,
                        "contract": requirement.contract,
                        "required": requirement.required,
                        "inject": requirement.inject,
                    }
                    for requirement in contribution.manifest.requires
                ],
            }
            for contribution in inspection.contributions
        ],
    }


def _sse(event: str, data: Any) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def _plugin_http_error(exc: Exception, *, install: bool = False) -> HTTPException:
    message = str(exc)
    if install and ("already installed" in message or "already exists" in message):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)


app = create_app()
