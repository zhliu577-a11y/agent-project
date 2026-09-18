# tests/test_context.py —— 上下文模块：token 估算与历史裁剪
import pytest

from core.context import (
    ContextRequest,
    MemoryTailWindowPolicy,
    SummaryWindowPolicy,
    TailWindowPolicy,
    estimate_tokens,
    request_tokens,
    trim_history,
    valid_tool_call_sequence,
)
from core.types import Message, ToolCall


def _messages() -> list[Message]:
    return [
        Message(role="user", content="第一问"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="1", name="json__format", arguments={"text": "{}"})],
        ),
        Message(role="tool", content='{\n  "a": 1\n}', tool_call_id="1"),
        Message(role="assistant", content="第二答"),
        Message(role="user", content="第三问"),
    ]


def test_estimate_tokens_heuristic() -> None:
    assert estimate_tokens("hello") == 2  # 5 个 ASCII ≈ 2 token
    assert estimate_tokens("你好") == 2  # 非 ASCII 按 1 字符 1 token
    assert estimate_tokens("") == 0


def test_trim_keeps_tail_when_over_budget() -> None:
    messages = _messages()
    # 预算小到必须丢，但至少保留一条
    trimmed, dropped = trim_history(messages, max_tokens=10)
    assert dropped >= 1
    assert len(trimmed) <= len(messages)
    assert trimmed[-1] is messages[-1]  # 一定保住最近的 user


def test_trim_noop_within_budget() -> None:
    messages = _messages()
    trimmed, dropped = trim_history(messages, max_tokens=1_000_000)
    assert dropped == 0
    assert trimmed == messages


def test_trim_nonpositive_budget_drops_all() -> None:
    trimmed, dropped = trim_history(_messages(), max_tokens=0)
    assert trimmed == []
    assert dropped == 5


def test_tool_call_sequence_detects_orphans() -> None:
    assert valid_tool_call_sequence(_messages())
    assert not valid_tool_call_sequence([Message(role="tool", content="x", tool_call_id="1")])
    assert not valid_tool_call_sequence(
        [
            Message(
                role="assistant",
                content="",
                tool_calls=[ToolCall(id="1", name="x", arguments={})],
            )
        ]
    )


@pytest.mark.asyncio
async def test_tail_window_policy_preserves_system_and_tool_pair() -> None:
    messages = [Message(role="system", content="system"), *_messages()]
    result = await TailWindowPolicy().prepare(
        ContextRequest(
            messages=messages,
            tools=[],
            max_tokens=1,
        )
    )

    assert result.messages[0].role == "system"
    assert result.messages[-1].role == "user"
    assert valid_tool_call_sequence(result.messages)


def test_request_tokens_includes_tool_schemas() -> None:
    messages = [Message(role="system", content="system")]
    schemas = [{"type": "function", "function": {"name": "echo"}}]
    assert request_tokens(messages, schemas) > request_tokens(messages, [])


@pytest.mark.asyncio
async def test_summary_window_policy_summarizes_old_messages_within_budget() -> None:
    messages = [
        Message(role="system", content="system"),
        *[Message(role="user", content=f"old message {index}") for index in range(30)],
        Message(role="user", content="latest question"),
    ]
    result = await SummaryWindowPolicy().prepare(
        ContextRequest(
            messages=messages,
            tools=[],
            max_tokens=80,
        )
    )

    assert result.messages[0].role == "system"
    assert "Earlier conversation summary:" in result.messages[0].content
    assert result.messages[-1].content == "latest question"
    assert result.dropped_count > 0
    assert result.summary
    assert request_tokens(result.messages, []) <= 80
    assert valid_tool_call_sequence(result.messages)


@pytest.mark.asyncio
async def test_summary_window_policy_is_noop_within_budget() -> None:
    messages = [Message(role="system", content="system"), Message(role="user", content="hello")]
    result = await SummaryWindowPolicy().prepare(
        ContextRequest(messages=messages, tools=[], max_tokens=1000)
    )

    assert result.messages == messages
    assert result.dropped_count == 0
    assert result.summary == ""


@pytest.mark.asyncio
async def test_memory_tail_window_injects_bounded_recall_context() -> None:
    original_system = Message(role="system", content="system")
    request = ContextRequest(
        messages=[
            original_system,
            Message(role="user", content="what project rules apply?"),
        ],
        tools=[],
        max_tokens=200,
        state={
            "memory.records": [
                {
                    "id": "m1",
                    "content": "The project uses ruff for linting.",
                    "kind": "preference",
                    "tags": ["project", "style"],
                }
            ]
        },
    )

    result = await MemoryTailWindowPolicy().prepare(request)

    assert result.metadata["strategy"] == "memory-tail-window"
    assert result.metadata["memoryRecords"] == 1
    assert "The project uses ruff for linting." in result.messages[0].content
    assert result.messages[0] is not original_system
    assert "memory.records" not in original_system.metadata
    assert request_tokens(result.messages, []) <= request.max_tokens


@pytest.mark.asyncio
async def test_memory_tail_window_without_records_uses_tail_window() -> None:
    request = ContextRequest(
        messages=[
            Message(role="system", content="system"),
            Message(role="user", content="hello"),
        ],
        tools=[],
        max_tokens=1000,
    )

    result = await MemoryTailWindowPolicy().prepare(request)

    assert result.metadata["strategy"] == "tail-window"
