from pathlib import Path

import pytest

from core.compaction import CompactionPolicy, CompactionRequest
from core.types import Message
from plugins.loader import load_compaction_plugins

REPO_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _history(count: int = 40) -> list[Message]:
    return [
        Message(
            role="user" if index % 2 == 0 else "assistant",
            content=f"message {index}: " + ("context " * 20),
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_rolling_summary_compacts_old_history() -> None:
    policy = next(
        plugin
        for plugin in load_compaction_plugins(REPO_PLUGINS)
        if plugin.manifest.name == "rolling-summary"
    ).create()
    messages = _history(400)

    result = await policy.compact(
        CompactionRequest(
            session_id="unit",
            messages=messages,
            max_tokens=500,
            revision=3,
        )
    )

    assert isinstance(policy, CompactionPolicy)
    assert result.compacted is True
    assert result.messages[0].role == "system"
    assert result.messages[0].metadata["compaction"] == "summary"
    assert result.messages[-1].id == messages[-1].id
    assert len(result.messages) < len(messages)


@pytest.mark.asyncio
async def test_rolling_summary_is_noop_below_threshold() -> None:
    policy = next(
        plugin
        for plugin in load_compaction_plugins(REPO_PLUGINS)
        if plugin.manifest.name == "rolling-summary"
    ).create()
    messages = [Message(role="user", content="short")]

    result = await policy.compact(
        CompactionRequest(
            session_id="unit",
            messages=messages,
            max_tokens=500,
            revision=0,
        )
    )

    assert result.compacted is False
    assert result.messages == messages
    assert result.metadata["reason"] == "below-threshold"


def test_repository_compaction_plugins_are_loadable() -> None:
    plugins = load_compaction_plugins(REPO_PLUGINS)

    assert [plugin.manifest.name for plugin in plugins] == ["rolling-summary"]
    assert plugins[0].manifest.contract == "compaction.v1"
