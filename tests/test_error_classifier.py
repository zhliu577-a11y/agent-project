# tests/test_error_classifier.py —— 错误边界分类器：翻译、透传与消费方
import asyncio
import sqlite3
from pathlib import Path

import pytest

from core.errors import (
    DeclaredPluginError,
    ModelError,
    PluginError,
    RetryableError,
    ToolError,
    boundary,
    classify_error,
    is_retryable,
    translate_error,
)
from core.hooks import HookGateway
from core.memory import MemoryNote, MemoryStore
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.session import SessionStore
from core.tool import Tool
from core.types import Message, ModelResponse, ToolCall
from gateways.mcp_gateway import McpGateway
from gateways.memory_gateway import MemoryGateway
from gateways.session_gateway import SessionGateway
from loop import run_agent
from plugins.loader import McpPluginSpec, PluginManifest


class FakeStatusError(Exception):
    """模拟带 status_code 的上游 API 错误（鸭子类型，不依赖 openai）。"""

    def __init__(self, status: int) -> None:
        self.status_code = status
        super().__init__(f"status {status}")


class FakeTimeoutError(Exception):
    pass


class FakeConnectionError(Exception):
    pass


def test_classify_status_codes() -> None:
    assert isinstance(classify_error(FakeStatusError(429)), RetryableError)
    assert isinstance(classify_error(FakeStatusError(503)), RetryableError)
    assert isinstance(classify_error(FakeStatusError(400)), ModelError)


def test_classify_timeout_connection_sqlite_and_oserror() -> None:
    assert isinstance(classify_error(TimeoutError("slow")), RetryableError)
    assert isinstance(classify_error(FakeTimeoutError()), RetryableError)
    assert isinstance(classify_error(FakeConnectionError()), RetryableError)
    assert isinstance(classify_error(sqlite3.OperationalError("db")), ToolError)
    assert isinstance(classify_error(OSError("file")), PluginError)


def test_classify_passes_through_control_and_unknown_errors() -> None:
    cancelled = asyncio.CancelledError()
    assert classify_error(cancelled) is cancelled
    unknown = ValueError("x")
    assert classify_error(unknown) is unknown
    already = ToolError("t")
    assert classify_error(already) is already


def test_is_retryable_uses_classification() -> None:
    assert is_retryable(FakeStatusError(429)) is True
    assert is_retryable(FakeStatusError(400)) is False
    assert is_retryable(ValueError("x")) is False


def test_declared_retryable_error_is_retryable() -> None:
    error = DeclaredPluginError("rate_limited", "上游限流", category="retryable", hint="稍后重试")
    assert is_retryable(error) is True


class _DeclaredFailTool(Tool):
    name = "declared_fail"
    description = "抛声明错误的工具"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise DeclaredPluginError(
            "invalid_json", "JSON 解析失败", category="tool", hint="检查 JSON 语法"
        )


@pytest.mark.asyncio
async def test_loop_feedback_includes_code_and_hint() -> None:
    model = _ScriptModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="declared_fail", arguments={})],
            ),
            ModelResponse(content="我修参数", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(_DeclaredFailTool())

    ctx = await run_agent(model, tools, HookGateway(), "系统", "试试")
    feedback = [m.content for m in ctx.messages if "code: invalid_json" in m.content]
    assert feedback, "工具失败反馈应带 code"
    assert "提示: 检查 JSON 语法" in feedback[0]


def test_translate_error_keeps_cause_and_context() -> None:
    original = sqlite3.OperationalError("disk")
    try:
        raise translate_error(original, context="写入失败") from original
    except ToolError as exc:
        assert "写入失败" in str(exc)
        assert exc.category == "tool"
        assert exc.__cause__ is original


@pytest.mark.asyncio
async def test_boundary_decorator_translates_method_errors() -> None:
    class Thing:
        @boundary("操作失败", fallback=ToolError)
        async def run(self) -> None:
            raise sqlite3.OperationalError("boom")

    with pytest.raises(ToolError, match="操作失败"):
        await Thing().run()


class _BadMemoryStore(MemoryStore):
    async def list_notes(self) -> list[MemoryNote]:
        raise sqlite3.OperationalError("db down")

    async def add_note(self, content: str, tags: list[str]) -> MemoryNote:
        raise sqlite3.OperationalError("db down")

    async def delete_note(self, note_id: str) -> bool:
        raise sqlite3.OperationalError("db down")

    async def update_note(
        self, note_id: str, content: str | None = None, tags: list[str] | None = None
    ) -> MemoryNote | None:
        raise sqlite3.OperationalError("db down")

    async def search_notes(self, query: str) -> list[MemoryNote]:
        raise sqlite3.OperationalError("db down")


@pytest.mark.asyncio
async def test_memory_gateway_translates_store_failure() -> None:
    gateway = MemoryGateway(_BadMemoryStore())
    with pytest.raises(ToolError, match="写入长期记忆失败"):
        await gateway.remember("x")


class _BadSessionStore(SessionStore):
    async def load(self, session_id: str):
        raise OSError("disk gone")

    async def save(self, session_id: str, messages: list[Message]) -> None:
        raise OSError("disk gone")

    async def load_checkpoint(self, session_id: str):
        raise OSError("disk gone")

    async def save_checkpoint(self, session_id: str, snapshot: dict) -> None:
        raise OSError("disk gone")

    async def delete_checkpoint(self, session_id: str) -> None:
        raise OSError("disk gone")


@pytest.mark.asyncio
async def test_session_gateway_translates_store_failure() -> None:
    gateway = SessionGateway(_BadSessionStore(), session_id="s")
    with pytest.raises(PluginError, match="读取会话历史失败"):
        await gateway.load_history()


def _spec(name: str = "time") -> McpPluginSpec:
    manifest = PluginManifest(
        name=name,
        type="mcp",
        version="",
        description="",
        enabled=True,
        directory=Path("."),
        entry={},
    )
    return McpPluginSpec(manifest=manifest, transport="stdio", command="x", args=[])


class _TimeoutGateway(McpGateway):
    async def connect_plugin(self, spec):
        raise TimeoutError("child process slow")


@pytest.mark.asyncio
async def test_mcp_mount_classifies_connect_failure() -> None:
    gateway = _TimeoutGateway([_spec()])
    with pytest.raises(RetryableError, match="连接失败"):
        await gateway.mount("time")


class _TimeoutModel(ModelAdapter):
    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        raise FakeTimeoutError("model slow")


@pytest.mark.asyncio
async def test_loop_records_model_error_category() -> None:
    ctx = await run_agent(_TimeoutModel(), ToolRegistry(), HookGateway(), "系统", "你好")
    assert ctx.stop_reason == "error"
    assert ctx.state["last_error"]["category"] == "retryable"


class _FailTool(Tool):
    name = "fail_tool"
    description = "总是抛数据库错误"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        raise sqlite3.OperationalError("db down")


class _ScriptModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


@pytest.mark.asyncio
async def test_loop_tool_failure_includes_category_for_agent() -> None:
    model = _ScriptModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="fail_tool", arguments={})],
            ),
            ModelResponse(content="我换一种方式", tool_calls=[]),
        ]
    )
    tools = ToolRegistry()
    tools.register(_FailTool())

    ctx = await run_agent(model, tools, HookGateway(), "系统", "试试")
    assert any("错误类别: tool" in message.content for message in ctx.messages)
