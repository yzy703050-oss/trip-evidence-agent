"""Generation options and native SDK stream boundaries; all clients are injected."""

from types import SimpleNamespace
from types import SimpleNamespace as Obj

import pytest


def test_model_options_disable_deepseek_by_default_and_do_not_leak_mutations():
    from trip_assistant.settings import generation_options

    first = generation_options("deepseek-flash", temperature=0.7, max_tokens=8192)
    assert first["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in first
    first["extra_body"]["thinking"]["type"] = "enabled"
    assert generation_options("deepseek-flash")["extra_body"]["thinking"]["type"] == "disabled"


def test_default_output_budget_allows_a_complete_complex_trip_patch():
    from trip_assistant.settings import Settings, generation_options

    settings = Settings(_env_file=None)
    assert settings.llm_max_tokens == 8192
    assert generation_options(settings.llm_model)["max_tokens"] == 8192


def test_application_disables_reasoning_even_with_legacy_low_setting(tmp_path, monkeypatch):
    import trip_assistant.bootstrap as bootstrap
    from trip_assistant.settings import Settings

    class CapturedModel:
        def __init__(self, *args, **kwargs):
            self.options = kwargs["generate_kwargs"]

    monkeypatch.setattr(bootstrap, "OpenAIChatModel", CapturedModel)
    app = bootstrap.create_application(
        Settings(_env_file=None, llm_thinking="low"), providers={}, storage_root=tmp_path
    )
    assert app.raw_model.options["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in app.raw_model.options
    assert app.preference_agent.model is not None


def test_other_provider_does_not_receive_deepseek_specific_options():
    from trip_assistant.settings import generation_options

    assert generation_options("other-model", temperature=0, max_tokens=5) == {
        "temperature": 0,
        "max_tokens": 5,
    }


def test_eval_override_can_enable_thinking_without_conflicting_disabled_flag():
    from trip_assistant.settings import generation_options

    value = generation_options("deepseek-flash", thinking="low")
    assert (
        value["extra_body"] == {"thinking": {"type": "enabled"}}
        and value["reasoning_effort"] == "low"
    )


# model_io


@pytest.mark.asyncio
async def test_stream_uses_final_snapshot():
    from trip_assistant.llm.messages import (
        collect_model_turn,
        to_assistant_tool_message,
        to_tool_message,
    )

    async def stream():
        yield SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "a",
                    "name": "train_search",
                    "input": {},
                    "raw_input": '{"departure_date":',
                }
            ]
        )
        yield SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "a",
                    "name": "train_search",
                    "input": {},
                    "raw_input": '{"departure_date":"2026-10-10"}',
                }
            ]
        )

    turn = await collect_model_turn(stream())
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0]["arguments"]["departure_date"] == "2026-10-10"
    assert to_assistant_tool_message(turn)["tool_calls"][0]["id"] == "a"
    assert to_tool_message("a", {"status": "ok"})["tool_call_id"] == "a"


@pytest.mark.asyncio
async def test_invalid_raw_tool_json_is_rejected():
    from trip_assistant.llm.messages import collect_model_turn

    response = SimpleNamespace(
        content=[
            {
                "type": "tool_use",
                "id": "a",
                "name": "train_search",
                "input": {"date": "fixed"},
                "raw_input": '{"date":',
            }
        ]
    )
    with pytest.raises(ValueError):
        await collect_model_turn(response)


@pytest.mark.asyncio
async def test_cumulative_text_is_not_duplicated():
    from trip_assistant.llm.messages import collect_model_turn

    async def stream():
        yield SimpleNamespace(content=[{"type": "text", "text": "hel"}])
        yield SimpleNamespace(
            content=[{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "hello"}]
        )

    assert (await collect_model_turn(stream())).text == "hello"


# project_model_runtime


def test_incomplete_output_keeps_finish_reason_and_usage_without_executable_content():
    from trip_assistant.llm.client import _snapshot

    with pytest.raises(ValueError) as caught:
        _snapshot(
            "unfinished", [], "length", {"prompt_tokens": 13, "completion_tokens": 1116}, None
        )
    assert caught.value.finish_reason == "length"
    assert caught.value.usage["input_tokens"] == 13
    assert caught.value.usage["output_tokens"] == 1116
    assert not hasattr(caught.value, "content")


def test_invalid_native_tool_json_keeps_usage_and_cannot_become_executable_content():
    from trip_assistant.llm.client import _snapshot

    calls = [{"id": "bad", "name": "write_trip", "arguments": '{"tripplan":'}]
    with pytest.raises(ValueError) as caught:
        _snapshot("", calls, "tool_calls", {"prompt_tokens": 9, "completion_tokens": 30}, None)
    assert caught.value.usage == {
        "input_tokens": 9,
        "output_tokens": 30,
        "cached_input_tokens": None,
        "uncached_input_tokens": None,
    }
    assert caught.value.finish_reason == "tool_calls"
    assert not hasattr(caught.value, "content")


def chunk(text=None, tools=None, finish=None, usage=None):
    return Obj(
        choices=[Obj(delta=Obj(content=text, tool_calls=tools), finish_reason=finish)]
        if usage is None
        else [],
        usage=usage,
    )


@pytest.mark.asyncio
async def test_stream_complete_tools_usage_and_parameters():
    from trip_assistant.llm.client import OpenAIChatModel
    from trip_assistant.llm.messages import collect_model_turn

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)

        async def stream():
            yield chunk(
                "你好", [Obj(index=0, id="c1", function=Obj(name="weather_", arguments='{"city":'))]
            )
            yield chunk(
                None,
                [Obj(index=0, id=None, function=Obj(name="query", arguments='"上海"}'))],
                "tool_calls",
            )
            yield chunk(usage=Obj(prompt_tokens=12, completion_tokens=8))

        return stream()

    client = Obj(chat=Obj(completions=Obj(create=create)))
    model = OpenAIChatModel("deepseek-test", client=client, generate_kwargs={"temperature": 0})
    response = await model([{"role": "user", "content": "天气"}], tools=[{"type": "function"}])
    turn = await collect_model_turn(response)
    assert turn.text == "你好"
    assert turn.tool_calls == [{"id": "c1", "name": "weather_query", "arguments": {"city": "上海"}}]
    assert response.usage == {
        "input_tokens": 12,
        "output_tokens": 8,
        "cached_input_tokens": None,
        "uncached_input_tokens": None,
    }
    assert calls[0]["temperature"] == 0 and calls[0]["stream_options"]["include_usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bad_json", "interrupted", "unfinished"])
async def test_incomplete_or_invalid_stream_never_returns_executable_snapshot(kind):
    from trip_assistant.llm.client import OpenAIChatModel

    async def create(**kwargs):
        async def stream():
            yield chunk(None, [Obj(index=0, id="a", function=Obj(name="query", arguments="{"))])
            if kind == "interrupted":
                raise ConnectionError("stream failed")
            if kind == "bad_json":
                yield chunk(finish="tool_calls")

        return stream()

    client = Obj(chat=Obj(completions=Obj(create=create)))
    with pytest.raises((ValueError, ConnectionError)):
        await OpenAIChatModel("test", client=client)([])


def test_message_protocol_is_independent():
    from trip_assistant.llm.messages import Msg

    msg = Msg("user", [{"type": "text", "text": "你好"}], "user")
    assert msg.role == "user" and msg.content[0]["text"] == "你好"


def test_entrypoints_work_when_framework_import_is_forbidden():
    import subprocess
    import sys
    from pathlib import Path

    code = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname == 'agentscope' or fullname.startswith('agentscope.'):
            raise ImportError('framework must not be imported')
sys.meta_path.insert(0, Block())
from trip_assistant.cli import TripEvidenceCLI
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import OpenAIChatModel
import trip_assistant.runtime.resilience
from trip_assistant.agents.main import MainAgent
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
