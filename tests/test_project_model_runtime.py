from types import SimpleNamespace as Obj

import pytest


def chunk(text=None, tools=None, finish=None, usage=None):
    return Obj(
        choices=[Obj(delta=Obj(content=text, tool_calls=tools), finish_reason=finish)]
        if usage is None
        else [],
        usage=usage,
    )


@pytest.mark.asyncio
async def test_stream_complete_tools_usage_and_parameters():
    from agents.model_io import collect_model_turn
    from agents.openai_model import OpenAIChatModel

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
    assert response.usage == {"input_tokens": 12, "output_tokens": 8}
    assert calls[0]["temperature"] == 0 and calls[0]["stream_options"]["include_usage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bad_json", "interrupted", "unfinished"])
async def test_incomplete_or_invalid_stream_never_returns_executable_snapshot(kind):
    from agents.openai_model import OpenAIChatModel

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
    from agents.runtime import AgentBase, Msg

    msg = Msg("user", [{"type": "text", "text": "你好"}], "user")
    assert msg.role == "user" and msg.content[0]["text"] == "你好"
    assert AgentBase(name="policy").name == "policy"


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
from cli import TripEvidenceCLI
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.openai_model import OpenAIChatModel
import utils.llm_resilience
from agents.main_agent import MainAgent
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
