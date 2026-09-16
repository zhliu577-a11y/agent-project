# tests/test_hooks.py —— 钩子网关：执行顺序、决策折叠与 ask-once 语义
import pytest

from core.hooks import HookGateway, LifecycleHooks, PromptRequest
from core.model import ModelAdapter
from core.registry import ToolRegistry
from core.tool import Tool
from core.types import ModelResponse, ToolCall, TurnContext
from loop import run_agent

pytestmark = pytest.mark.asyncio


class Recording(LifecycleHooks):
    """把每个事件按序记进共享列表，方便断言执行顺序。"""

    def __init__(self, name: str, trace: list[str], decision: str = "allow") -> None:
        self._name = name
        self._trace = trace
        self._decision = decision

    async def turn_start(self, ctx) -> None:
        self._trace.append(f"start:{self._name}")

    async def user_prompt_submit(self, request):
        self._trace.append(f"prompt:{self._name}")
        return self._decision

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


async def test_user_prompt_deny_wins_and_all_policies_see_the_request() -> None:
    trace: list[str] = []
    gateway = HookGateway()
    gateway.add(Recording("gate", trace, decision="deny"), priority=10)
    gateway.add(Recording("audit", trace), priority=100)

    allowed = await gateway.user_prompt_submit(PromptRequest("hello", session_id="s1"))

    assert allowed is False
    assert trace == ["prompt:gate", "prompt:audit"]


async def test_user_prompt_ask_confirms_once() -> None:
    gateway = HookGateway()
    gateway.add(Recording("policy-a", [], decision="ask"))
    gateway.add(Recording("policy-b", [], decision="ask"))
    confirmations: list[str] = []

    async def confirm(request) -> bool:
        confirmations.append(request.text)
        return True

    allowed = await gateway.user_prompt_submit(
        PromptRequest("hello", session_id="s1"),
        confirm=confirm,
    )

    assert allowed is True
    assert confirmations == ["hello"]


async def test_user_prompt_policy_exception_fails_closed() -> None:
    class Exploding(LifecycleHooks):
        async def user_prompt_submit(self, request):
            raise RuntimeError("boom")

    gateway = HookGateway()
    gateway.add(Exploding())

    assert await gateway.user_prompt_submit(PromptRequest("hello")) is False


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


class EchoTool(Tool):
    name = "echo"
    description = "回显"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "executed"


class AskGate(LifecycleHooks):
    async def tool_before(self, ctx, tool_call):
        return "ask"


class FakeModel(ModelAdapter):
    def __init__(self, script):
        self._script = list(script)

    async def complete(self, messages, tool_schemas, on_token=None) -> ModelResponse:
        return self._script.pop(0)


async def test_loop_passes_confirm_into_ask_fold() -> None:
    confirmations: list[str] = []

    async def confirm(ctx, tool_call) -> bool:
        confirmations.append(tool_call.name)
        return True

    model = FakeModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCall(id="1", name="echo", arguments={})],
            ),
            ModelResponse(content="完成", tool_calls=[]),
        ]
    )
    registry = ToolRegistry()
    registry.register(EchoTool())
    hooks = HookGateway()
    hooks.add(AskGate())

    ctx = await run_agent(model, registry, hooks, "你是助手", "执行", confirm=confirm)

    assert confirmations == ["echo"]
    assert any(message.content == "executed" for message in ctx.messages)
    assert ctx.stop_reason == "done"
