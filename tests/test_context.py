# tests/test_context.py —— 上下文模块：token 估算与历史裁剪
from core.context import estimate_tokens, trim_history
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
