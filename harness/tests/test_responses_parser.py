from types import SimpleNamespace

from core.parser import OpenAIResponsesParser


def _event(event_type: str, **values: object) -> SimpleNamespace:
    return SimpleNamespace(type=event_type, **values)


def test_responses_parser_collects_text_deltas() -> None:
    tokens: list[str] = []
    parser = OpenAIResponsesParser(on_token=tokens.append)

    parser.feed(_event("response.output_text.delta", delta="hello"))
    parser.feed(_event("response.output_text.delta", delta=" world"))

    response = parser.finalize()
    assert response.content == "hello world"
    assert response.tool_calls == []
    assert tokens == ["hello", " world"]


def test_responses_parser_collects_function_call_deltas() -> None:
    parser = OpenAIResponsesParser()
    item = SimpleNamespace(
        type="function_call",
        call_id="call_1",
        id="item_1",
        name="text__slugify",
        arguments="",
    )

    parser.feed(_event("response.output_item.added", output_index=1, item=item))
    parser.feed(
        _event(
            "response.function_call_arguments.delta",
            output_index=1,
            item_id="item_1",
            delta='{"text":',
        )
    )
    parser.feed(
        _event(
            "response.function_call_arguments.delta",
            output_index=1,
            item_id="item_1",
            delta='"Hello World"}',
        )
    )

    response = parser.finalize()
    assert response.content == ""
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "text__slugify"
    assert response.tool_calls[0].arguments == {"text": "Hello World"}
