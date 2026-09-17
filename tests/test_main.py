from types import SimpleNamespace

import pytest

import main as cli_main
from gateways.skill_gateway import SkillPermissionDecision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("y", True),
        ("YES", True),
        ("n", False),
        ("", False),
    ],
)
async def test_approve_skill_uses_explicit_yes_only(
    monkeypatch,
    answer: str,
    expected: bool,
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or answer)
    decision = SkillPermissionDecision(
        access="ask",
        reason="test policy",
        matched_pattern="review*",
        source="global",
    )

    approved = await cli_main._approve_skill("review", "templates/check.md", decision)

    assert approved is expected
    assert "review (templates/check.md)" in prompts[0]
    assert "'review*'" in prompts[0]


@pytest.mark.asyncio
async def test_main_injects_cli_skill_approver(monkeypatch) -> None:
    captured = {}
    config = SimpleNamespace(
        embedding_provider="debug",
        context_max_tokens=1234,
    )

    class FakeRuntime:
        def __init__(self, loaded_config, *, skill_approver=None):
            captured["config"] = loaded_config
            captured["skill_approver"] = skill_approver
            self.model = object()
            self.tools = object()
            self.hooks = object()
            self.system_prompt = "system"
            self.history = []
            self.session = None
            self.events = None
            self.context_gateway = None
            self.context_policy = None
            self.memory_extraction = None

        async def start(self) -> None:
            captured["started"] = True

        async def close(self) -> None:
            captured["closed"] = True

    async def fake_chat(*args, **kwargs) -> list:
        captured["chat"] = (args, kwargs)
        return []

    monkeypatch.setattr(cli_main, "load_dotenv", lambda: None)
    monkeypatch.setattr(cli_main, "setup_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_main.AppConfig, "load", classmethod(lambda cls: config))
    monkeypatch.setattr(cli_main, "HarnessRuntime", FakeRuntime)
    monkeypatch.setattr(cli_main, "chat", fake_chat)

    await cli_main.main()

    assert captured["config"] is config
    assert captured["skill_approver"] is cli_main._approve_skill
    assert captured["started"] is True
    assert captured["closed"] is True
