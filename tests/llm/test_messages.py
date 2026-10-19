from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_complete_stream_and_native_pairing():
    from trip_assistant.llm.messages import (
        collect_model_turn,
        to_assistant_tool_message,
        to_tool_message,
    )

    async def stream():
        yield SimpleNamespace(
            content=[{"type": "tool_use", "id": "c", "name": "read", "raw_input": '{"resource":'}]
        )
        yield SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "c",
                    "name": "read",
                    "raw_input": '{"resource":"preferences","request_kind":"lookup"}',
                }
            ]
        )

    turn = await collect_model_turn(stream())
    assert turn.tool_calls[0]["arguments"]["resource"] == "preferences"
    assert to_assistant_tool_message(turn)["tool_calls"][0]["function"]["name"] == "read"
    assert to_tool_message("c", {"status": "ok"})["tool_call_id"] == "c"


@pytest.mark.asyncio
async def test_partial_stream_never_becomes_executable():
    from trip_assistant.llm.messages import collect_model_turn

    async def stream():
        yield SimpleNamespace(
            content=[{"type": "tool_use", "id": "c", "name": "read", "raw_input": '{"resource":'}]
        )

    with pytest.raises(ValueError):
        await collect_model_turn(stream())


@pytest.mark.asyncio
async def test_duplicate_ids_rejected_and_cumulative_text_not_duplicated():
    from trip_assistant.llm.messages import collect_model_turn

    with pytest.raises(ValueError):
        await collect_model_turn(
            SimpleNamespace(
                content=[{"type": "tool_use", "id": "c", "name": "read", "input": {}}] * 2
            )
        )

    async def stream():
        yield SimpleNamespace(content=[{"type": "text", "text": "hel"}])
        yield SimpleNamespace(content=[{"type": "text", "text": "hello"}])

    assert (await collect_model_turn(stream())).text == "hello"
