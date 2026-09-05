# tests/test_hooks.py —— 钩子网关：执行顺序、决策折叠与 ask-once 语义
import pytest

from core.hooks import HookGateway, LifecycleHooks
from core.types import ToolCall, TurnContext

pytestmark = pytest.mark.asyncio


class Recording(LifecycleHooks):
    """把每个事件按序记进共享列表，方便断言执行顺序。"""

    def __init__(self, name: str, trace: list[str], decision: str = "allow") -> None:
        self._name = name
        self._trace = trace
        self._decision = decision

    async def turn_start(self, ctx) -> None:
        self._trace.append(f"start:{self._name}")

    async def tool_before(self, ctx, tool_call):
        self._trace.append(f"before:{self._name}")
        return self._decision

    async def tool_after(self, ctx, tool_call, result, ok) -> None:
        self._trace.append(f"after:{self._name}")


def _ctx() -> TurnContext:
    return TurnContext(messages=[])


async def test_hooks_execute_by_priority_then_registration_order() -> None:
    trace: list[str] = []
    gateway = HookGateway()
    gateway.add(Recording("late", trace), priority=100)
    gateway.add(Recording("early", trace), priority=10)
    gateway.add(Recording("middle-a", trace), priority=50)
    gateway.add(Recording("middle-b", trace), priority=50)

    await gateway.turn_start(_ctx())
    assert trace == ["start:early", "start:middle-a", "start:middle-b", "start:late"]


async def test_deny_wins_and_later_hooks_still_see_the_attempt() -> None:
    trace: list[str] = []
    gateway = HookGateway()
    gateway.add(Recording("gate", trace, decision="deny"), priority=10)
    gateway.add(Recording("audit", trace), priority=100)

    allowed = await gateway.tool_before(_ctx(), ToolCall(id="1", name="del", arguments={}))
    assert allowed is False
    # 全部钩子都参与了表态（deny 折叠为最终拒绝，但不短路后续审计钩子）
    assert trace == ["before:gate", "before:audit"]


async def test_ask_folds_and_confirms_once() -> None:
    trace: list[str] = []
    confirmations: list[str] = []

    async def confirm(ctx, tool_call) -> bool:
        confirmations.append(tool_call.name)
        return True

    gateway = HookGateway()
    gateway.add(Recording("policy-a", trace, decision="ask"))
    gateway.add(Recording("policy-b", trace, decision="ask"))

    allowed = await gateway.tool_before(
        _ctx(), ToolCall(id="1", name="write", arguments={}), confirm=confirm
    )
    assert allowed is True
    assert confirmations == ["write"]  # 两个 ask 只触发一次用户确认
    assert trace == ["before:policy-a", "before:policy-b"]


async def test_ask_declined_by_user() -> None:
    async def confirm(ctx, tool_call) -> bool:
        return False

    gateway = HookGateway()
    gateway.add(Recording("policy", [], decision="ask"))
    allowed = await gateway.tool_before(
        _ctx(), ToolCall(id="1", name="write", arguments={}), confirm=confirm
    )
    assert allowed is False


async def test_exception_counts_as_deny() -> None:
    class Exploding(LifecycleHooks):
        async def tool_before(self, ctx, tool_call):
            raise RuntimeError("boom")

    gateway = HookGateway()
    gateway.add(Exploding())
    assert await gateway.tool_before(_ctx(), ToolCall(id="1", name="x", arguments={})) is False


async def test_invalid_decision_counts_as_deny() -> None:
    gateway = HookGateway()
    gateway.add(Recording("weird", [], decision="maybe"))
    assert await gateway.tool_before(_ctx(), ToolCall(id="1", name="x", arguments={})) is False
