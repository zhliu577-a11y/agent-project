# api/main.py —— FastAPI 接口：chat / 会话历史 / 长期记忆 / 插件概览
#
# 启动：.venv\Scripts\python.exe -m api.main
# 或：uvicorn api.main:app --reload
#
# 说明：本文件是“无头 harness”，装配逻辑与 main.py（CLI）一致但独立维护；
# Web 模式没有交互式弹窗，ask 权限默认按拒绝处理（_web_confirm）。
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import AppConfig
from core.context import history_tokens, trim_history
from core.errors import AgentError
from core.hooks import HookGateway
from core.model import ModelAdapter
from core.prompt import build_system_prompt
from core.registry import ToolRegistry
from core.session import SessionStore
from core.tracing import begin_trace, current_trace_id, setup_logging
from core.types import message_to_dict
from gateways.mcp_gateway import McpGateway, UsePlugin
from gateways.memory_gateway import (
    ForgetTool,
    MemoryGateway,
    RecallTool,
    RememberTool,
    UpdateNoteTool,
)
from gateways.session_gateway import SessionGateway
from gateways.skill_gateway import SkillGateway, UseSkill
from loop import run_agent
from plugins.loader import PluginAssembly, assemble_plugins, registered_kinds

logger = logging.getLogger("api")

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_DIR = ROOT / "plugins"


# ---------- 请求/响应模型 ----------


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str
    max_context_tokens: int | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    stop_reason: str
    history_len: int


class NoteCreate(BaseModel):
    content: str
    tags: list[str] = []


class NoteOut(BaseModel):
    id: str
    content: str
    tags: list[str]
    created_at: str


# ---------- 运行时 ----------


@dataclass
class Runtime:
    assembly: PluginAssembly
    hooks: HookGateway
    model: ModelAdapter
    tools: ToolRegistry
    session_store: SessionStore
    memory_gateway: MemoryGateway
    skill_gateway: SkillGateway
    mcp_gateway: McpGateway
    system_prompt: str
    local_tool_names: list[str] = field(default_factory=list)

    async def close(self) -> None:
        await self.mcp_gateway.close()


def _select_plugin(plugins, kind_label: str, name: str):
    plugin = next((p for p in plugins if p.manifest.name == name), None)
    if plugin is None:
        available = [p.manifest.name for p in plugins]
        raise RuntimeError(f"未知的{kind_label}插件: {name}，可选: {available}")
    return plugin


async def build_runtime(
    plugins_dir: Path | None = None, config: AppConfig | None = None
) -> Runtime:
    """装配一次完整的无头 harness（等价于 main.py 的非交互部分）。"""
    plugins_dir = Path(plugins_dir or PLUGINS_DIR)
    config = config or AppConfig.load()
    os.environ.setdefault("EMBEDDING_PROVIDER", config.embedding_provider)
    assembly = assemble_plugins(plugins_dir)

    hooks = HookGateway()
    for _, hook in assembly.hooks:
        hooks.add(hook)

    model_plugin = _select_plugin(assembly.models, "模型", config.model)
    model = model_plugin.create()

    session_plugin = _select_plugin(assembly.sessions, "会话存储", config.session_store)
    session_store = session_plugin.create()

    memory_plugin = _select_plugin(assembly.memories, "长期记忆", config.memory_store)
    memory_gateway = MemoryGateway(memory_plugin.create())

    tools = ToolRegistry()
    local_tool_names: list[str] = []
    for _, tools_list in assembly.tools:
        for tool in tools_list:
            tools.register(tool)
            local_tool_names.append(tool.name)

    skill_gateway = SkillGateway(assembly.skills)
    tools.register(UseSkill(skill_gateway))
    tools.register(RememberTool(memory_gateway))
    tools.register(RecallTool(memory_gateway))
    tools.register(ForgetTool(memory_gateway))
    tools.register(UpdateNoteTool(memory_gateway))

    mcp_gateway = McpGateway(assembly.mcp)
    tools.register(UsePlugin(mcp_gateway, tools))

    runtime = Runtime(
        assembly=assembly,
        hooks=hooks,
        model=model,
        tools=tools,
        session_store=session_store,
        memory_gateway=memory_gateway,
        skill_gateway=skill_gateway,
        mcp_gateway=mcp_gateway,
        system_prompt="",
        local_tool_names=sorted(local_tool_names),
    )
    preload_parts: list[tuple[str, str]] = []
    for skill in assembly.skills:
        if not skill.preload:
            continue
        try:
            content = skill_gateway.get(skill.manifest.name)
        except ValueError as exc:
            logger.warning("预载技能 %s 读取失败，已跳过: %s", skill.manifest.name, exc)
            continue
        preload_parts.append((skill.manifest.name, content))
    runtime.system_prompt = build_system_prompt(
        mcp=[(spec.manifest.name, spec.manifest.description) for spec in assembly.mcp],
        skills=skill_gateway.catalog(),
        preloads=preload_parts,
    )
    return runtime


async def _web_confirm(ctx, tool_call) -> bool:
    """Web 模式没有弹窗：ask 工具默认拒绝（allow/deny 不受影响）。"""
    logger.warning("Web 模式收到 ask 工具调用，默认拒绝: %s", tool_call.name)
    return False


# ---------- FastAPI 应用 ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(logging.INFO)
    load_dotenv()
    config = AppConfig.load()
    app.state.config = config
    try:
        app.state.runtime = await build_runtime(config=config)
    except (RuntimeError, ValueError) as exc:
        logger.exception("Harness 启动失败")
        raise RuntimeError(f"Harness 启动失败: {exc}") from exc
    logger.info("FastAPI Harness 已就绪")
    yield
    await app.state.runtime.close()


app = FastAPI(title="agent-project Harness API", lifespan=lifespan)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """每次 HTTP 请求分配/沿用 trace id，日志可按链路串联。"""
    begin_trace(request.headers.get("x-trace-id"))
    response = await call_next(request)
    response.headers["x-trace-id"] = current_trace_id() or "-"
    return response


# 内部错误类别 → HTTP 状态码：接口层统一映射，不在每个端点里判断
_ERROR_STATUS = {
    "config": 400,
    "model": 502,
    "retryable": 503,
    "tool": 500,
    "plugin": 500,
    "agent": 500,
}


@app.exception_handler(AgentError)
async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
    status = _ERROR_STATUS.get(exc.category, 500)
    logger.warning("请求失败 category=%s: %s", exc.category, exc)
    return JSONResponse(
        status_code=status,
        content={"detail": str(exc), "category": exc.category},
    )


def _session(app_: FastAPI, session_id: str) -> SessionGateway:
    return SessionGateway(app_.state.runtime.session_store, session_id=session_id)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model": type(app.state.runtime.model).__name__}


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    runtime = app.state.runtime
    session = _session(app, req.session_id)
    history = await session.load_history()
    max_tokens = req.max_context_tokens or app.state.config.context_max_tokens
    trimmed, dropped = trim_history(history, max_tokens)
    if dropped:
        logger.warning("会话 %s 裁剪历史 %d 条", req.session_id, dropped)

    try:
        ctx = await run_agent(
            runtime.model,
            runtime.tools,
            runtime.hooks,
            runtime.system_prompt,
            req.message,
            history=trimmed,
            confirm=_web_confirm,
        )
    except Exception as exc:
        logger.exception("chat 执行失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"chat 执行失败: {exc}") from exc

    history = ctx.messages[1:]
    await session.save_history(history)
    reply = next(
        (m.content for m in reversed(ctx.messages) if m.role == "assistant" and m.content),
        ctx.messages[-1].content if ctx.messages else "",
    )
    return ChatResponse(
        session_id=req.session_id,
        reply=reply,
        stop_reason=ctx.stop_reason,
        history_len=len(history),
    )


@app.get("/sessions/{session_id}/history")
async def session_history(session_id: str) -> list[dict[str, Any]]:
    history = await _session(app, session_id).load_history()
    return [message_to_dict(message) for message in history]


@app.get("/sessions/{session_id}/checkpoint")
async def session_checkpoint(session_id: str) -> dict[str, Any]:
    snapshot = await _session(app, session_id).load_checkpoint()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="该会话还没有 checkpoint")
    return snapshot


@app.delete("/sessions/{session_id}")
async def session_clear(session_id: str) -> dict[str, Any]:
    session = _session(app, session_id)
    await session.save_history([])
    await session.delete_checkpoint()
    return {"session_id": session_id, "cleared": True}


@app.get("/context/{session_id}")
async def context_status(session_id: str) -> dict[str, Any]:
    history = await _session(app, session_id).load_history()
    max_tokens = app.state.config.context_max_tokens
    trimmed, dropped = trim_history(history, max_tokens)
    return {
        "session_id": session_id,
        "messages": len(history),
        "tokens": history_tokens(history),
        "would_trim": dropped,
        "max_context_tokens": max_tokens,
    }


@app.get("/memory/notes", response_model=list[NoteOut])
async def memory_list(query: str = "") -> list[NoteOut]:
    notes = await app.state.runtime.memory_gateway.recall(query)
    return [
        NoteOut(id=note.id, content=note.content, tags=note.tags, created_at=note.created_at)
        for note in notes
    ]


@app.post("/memory/notes", response_model=NoteOut, status_code=201)
async def memory_add(note: NoteCreate) -> NoteOut:
    saved = await app.state.runtime.memory_gateway.remember(note.content, note.tags)
    return NoteOut(id=saved.id, content=saved.content, tags=saved.tags, created_at=saved.created_at)


@app.delete("/memory/notes/{note_id}")
async def memory_delete(note_id: str) -> dict[str, Any]:
    removed = await app.state.runtime.memory_gateway.forget(note_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"未找到笔记 {note_id}")
    return {"deleted": note_id}


@app.get("/plugins")
async def plugin_overview() -> dict[str, Any]:
    assembly = app.state.runtime.assembly
    packages = sorted({contribution.package_name for contribution in assembly.contributions})
    kinds: dict[str, list[str]] = {kind: [] for kind in registered_kinds()}
    for contribution in assembly.contributions:
        kinds.setdefault(contribution.kind, []).append(contribution.manifest.name)
    return {
        "packages": packages,
        "contributions": [
            {
                "package": contribution.package_name,
                "id": contribution.contribution_id,
                "kind": contribution.kind,
                "name": contribution.manifest.name,
            }
            for contribution in assembly.contributions
        ],
        "kinds": kinds,
        "local_tools": app.state.runtime.local_tool_names,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
    )
