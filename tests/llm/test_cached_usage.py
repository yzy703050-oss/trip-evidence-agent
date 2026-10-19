from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "extra,hit,miss",
    [
        ({"prompt_cache_hit_tokens": 30, "prompt_cache_miss_tokens": 70}, 30, 70),
        ({"prompt_tokens_details": SimpleNamespace(cached_tokens=40)}, 40, 60),
        ({}, None, None),
    ],
)
def test_adapter_preserves_provider_cached_usage_without_double_counting(extra, hit, miss):
    from trip_assistant.llm.client import _usage

    usage = _usage(SimpleNamespace(prompt_tokens=100, completion_tokens=12, **extra))
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 12
    assert usage["cached_input_tokens"] == hit and usage["uncached_input_tokens"] == miss


@pytest.mark.asyncio
async def test_cached_usage_reaches_persisted_telemetry(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    model = ScriptedModel(
        [
            SimpleNamespace(
                content=[{"type": "text", "text": "你好"}],
                usage={
                    "input_tokens": 100,
                    "output_tokens": 12,
                    "cached_input_tokens": 30,
                    "uncached_input_tokens": 70,
                },
            )
        ]
    )
    app = create_application(
        Settings(_env_file=None), model=model, providers={}, storage_root=tmp_path, user_id="a"
    )
    result = await app.run("你好")
    row = next(
        r for r in app.sessions.read_runs(turn_id=result["turn_id"]) if r["type"] == "model_call"
    )
    assert row["cached_input_tokens"] == 30 and row["uncached_input_tokens"] == 70
