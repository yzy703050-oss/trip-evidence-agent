from types import SimpleNamespace as Obj

import pytest

from trip_assistant.llm.client import IncompleteModelResponse, OpenAIChatModel, _snapshot, _usage


def test_reasoning_usage_is_a_subset_and_absent_detail_remains_unknown():
    raw = {
        "prompt_tokens": 20,
        "completion_tokens": 30,
        "completion_tokens_details": {"reasoning_tokens": 25},
    }
    assert _usage(raw)["reasoning_tokens"] == 25
    assert _usage(raw)["output_tokens"] == 30
    assert _usage({"completion_tokens": 30}).get("reasoning_tokens") is None


def test_length_only_retains_safe_counts_and_never_executable_content():
    with pytest.raises(IncompleteModelResponse) as caught:
        _snapshot(
            "secret-body",
            [{"arguments": "secret-args"}],
            "length",
            {"completion_tokens": 8192},
            None,
        )
    error = caught.value
    assert error.diagnostics["text_chars"] == 11
    assert error.diagnostics["tool_arguments_chars"] == 11
    assert not hasattr(error, "content")
    assert "secret-body" not in str(vars(error)) and "secret-args" not in str(vars(error))


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_model_counts_reasoning_without_retaining_reasoning_text(streaming):
    async def create(**kwargs):
        usage = Obj(
            prompt_tokens=4, completion_tokens=10, completion_tokens_details=Obj(reasoning_tokens=8)
        )
        if not streaming:
            return Obj(
                choices=[
                    Obj(
                        message=Obj(
                            content="ok", reasoning_content="private-reason", tool_calls=[]
                        ),
                        finish_reason="stop",
                    )
                ],
                usage=usage,
            )

        async def chunks():
            yield Obj(
                choices=[
                    Obj(
                        delta=Obj(
                            content=None, reasoning_content="private-reason", tool_calls=None
                        ),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )
            yield Obj(
                choices=[Obj(delta=Obj(content="ok", tool_calls=None), finish_reason="stop")],
                usage=usage,
            )

        return chunks()

    model = OpenAIChatModel(
        "deepseek-test", client=Obj(chat=Obj(completions=Obj(create=create))), stream=streaming
    )
    response = await model([])
    assert response.usage["reasoning_tokens"] == 8
    assert response.diagnostics["reasoning_chars"] == len("private-reason")
    assert "private-reason" not in str(response)
