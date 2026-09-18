# tests/test_parser.py —— Response Parser：流式文本、工具调用分片与边界情况
import json
from dataclasses import dataclass, field

import pytest

from core.parser import OpenAICompatibleParser, ToolCallAccumulator


@dataclass
class FakeFunction:
    name: str | None = None
    arguments: str | None = None


@dataclass
class FakeToolCall:
    index: int
    id: str | None = None
    function: FakeFunction | None = None


@dataclass
class FakeDelta:
    content: str | None = None
    tool_calls: list[FakeToolCall] | None = field(default=None)


@dataclass
class FakeChoice:
    delta: FakeDelta


@dataclass
class FakeChunk:
    choices: list[FakeChoice] = field(default_factory=list)


def test_text_stream_calls_on_token_in_order() -> None:
    received: list[str] = []
    parser = OpenAICompatibleParser(on_token=received.append)

    parser.feed(FakeChunk([FakeChoice(FakeDelta(content="你好"))]))
    parser.feed(FakeChunk([FakeChoice(FakeDelta(content="，世界"))]))
    result = parser.finalize()

    assert received == ["你好", "，世界"]
    assert result.content == "你好，世界"
    assert result.tool_calls == []


def test_tool_call_fragments_merge_and_sort_by_index() -> None:
    parser = OpenAICompatibleParser()

    # index 1 先到，且 id/name/arguments 分片；index 0 后到
    parser.feed(
        FakeChunk(
            [
                FakeChoice(
                    FakeDelta(
                        tool_calls=[
                            FakeToolCall(index=1, id="call_b", function=FakeFunction(name="get_"))
                        ]
                    )
                )
            ]
        )
    )
    parser.feed(
        FakeChunk(
            [
                FakeChoice(
                    FakeDelta(
                        tool_calls=[
                            FakeToolCall(
                                index=1, function=FakeFunction(name="time", arguments='{"zone":')
                            )
                        ]
                    )
                )
            ]
        )
    )
    parser.feed(
        FakeChunk(
            [
                FakeChoice(
                    FakeDelta(
                        tool_calls=[
                            FakeToolCall(index=1, function=FakeFunction(arguments='"UTC"}')),
                            FakeToolCall(
                                index=0,
                                id="call_a",
                                function=FakeFunction(name="echo", arguments="{}"),
                            ),
                        ]
                    )
                )
            ]
        )
    )

    calls = parser.finalize().tool_calls
    assert [call.name for call in calls] == ["echo", "get_time"]  # 按 index 排序
    assert calls[1].arguments == {"zone": "UTC"}
    assert calls[1].id == "call_b"


def test_tool_call_suppresses_later_text_callback_but_keeps_content() -> None:
    received: list[str] = []
    parser = OpenAICompatibleParser(on_token=received.append)

    parser.feed(
        FakeChunk(
            [
                FakeChoice(
                    FakeDelta(
                        tool_calls=[FakeToolCall(index=0, id="c", function=FakeFunction(name="t"))]
                    )
                )
            ]
        )
    )
    parser.feed(FakeChunk([FakeChoice(FakeDelta(content="工具之后的文本"))]))

    result = parser.finalize()
    assert received == []
    assert result.content == "工具之后的文本"


def test_empty_choices_and_missing_delta_are_ignored() -> None:
    parser = OpenAICompatibleParser()
    parser.feed(FakeChunk([]))
    parser.feed(FakeChunk([FakeChoice(FakeDelta())]))
    result = parser.finalize()
    assert result.content == ""
    assert result.tool_calls == []


def test_tool_call_without_id_falls_back_to_index_name() -> None:
    accumulator = ToolCallAccumulator()
    accumulator.add(2, name="echo", arguments="{}")
    calls = accumulator.finalize()
    assert calls[0].id == "call_2"


def test_invalid_arguments_json_raises() -> None:
    accumulator = ToolCallAccumulator()
    accumulator.add(0, name="echo", arguments='{"broken":')
    with pytest.raises(json.JSONDecodeError):
        accumulator.finalize()
