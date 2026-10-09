from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_stream_uses_final_snapshot():
    from agents.model_io import collect_model_turn, to_assistant_tool_message, to_tool_message
    async def stream():
        yield SimpleNamespace(content=[{"type": "tool_use", "id": "a", "name": "train_search",
                                       "input": {}, "raw_input": '{"departure_date":'}])
        yield SimpleNamespace(content=[{"type": "tool_use", "id": "a", "name": "train_search",
                                       "input": {}, "raw_input": '{"departure_date":"2026-10-10"}'}])
    turn = await collect_model_turn(stream())
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["arguments"]["departure_date"] == "2026-10-10"
    assert to_assistant_tool_message(turn)["tool_calls"][0]["id"] == "a"
    assert to_tool_message("a", {"status": "ok"})["tool_call_id"] == "a"


@pytest.mark.asyncio
async def test_invalid_raw_tool_json_is_rejected():
    from agents.model_io import collect_model_turn
    response = SimpleNamespace(content=[{"type": "tool_use", "id": "a", "name": "train_search",
                                        "input": {"date": "fixed"}, "raw_input": '{"date":'}])
    with pytest.raises(ValueError):
        await collect_model_turn(response)


@pytest.mark.asyncio
async def test_cumulative_text_is_not_duplicated():
    from agents.model_io import collect_model_turn
    async def stream():
        yield SimpleNamespace(content=[{"type": "text", "text": 'hel'}])
        yield SimpleNamespace(content=[{"type": "thinking", "thinking": "private"},
                                       {"type": "text", "text": 'hello'}])
    assert (await collect_model_turn(stream())).text == "hello"
