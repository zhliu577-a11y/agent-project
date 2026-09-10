# tests/test_state.py —— 状态快照（轻量 checkpoint 数据层）
from core.state import capture
from core.types import Message, TurnContext


def test_capture_makes_json_safe_snapshot_without_messages() -> None:
    ctx = TurnContext(
        messages=[Message(role="user", content="hi")],
        turn=3,
        stop_reason="done",
        state={"fail_counts": {"tool_x": 2}, "blocked_tools": {"tool_x"}},
    )
    snapshot = capture(ctx)
    assert snapshot["turn"] == 3
    assert snapshot["stop_reason"] == "done"
    assert snapshot["state"]["fail_counts"] == {"tool_x": 2}
    assert snapshot["state"]["blocked_tools"] == ["tool_x"]  # set -> list
    assert "updated_at" in snapshot
    assert "messages" not in snapshot
