from datetime import UTC
from types import SimpleNamespace

import pytest


async def test_network_retries_record_each_attempt_under_one_logical_call(tmp_path):
    from trip_assistant.bootstrap import create_application
    from trip_assistant.llm.client import ModelResponse
    from trip_assistant.settings import Settings

    class Flaky:
        calls = 0

        async def __call__(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                error = ConnectionError("temporary")
                error.usage = {"input_tokens": 7, "output_tokens": 1}
                raise error
            return ModelResponse(
                [{"type": "text", "text": "你好"}], {"input_tokens": 13, "output_tokens": 2}
            )

    model = Flaky()
    app = create_application(
        Settings(_env_file=None, extra_retries=1), model=model, providers={}, storage_root=tmp_path
    )
    result = await app.run("你好")
    rows = [r for r in app.sessions.read_runs() if r["type"] == "model_call"]
    assert result["final_answer"] == "你好" and model.calls == 2
    assert len(rows) == 2 and [r["attempt"] for r in rows] == [1, 2]
    assert len({r["logical_call_id"] for r in rows}) == 1
    assert [r["status"] for r in rows] == ["error", "success"]
    assert sum(r["input_tokens"] for r in rows) == 20
    assert sum(r["output_tokens"] for r in rows) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 2])
async def test_truncated_model_attempt_keeps_usage_and_never_executes_partial_tools(
    tmp_path, retries
):
    from trip_assistant.bootstrap import create_application
    from trip_assistant.llm.client import IncompleteModelResponse
    from trip_assistant.settings import Settings

    class Truncated:
        async def __call__(self, messages, **kwargs):
            raise IncompleteModelResponse(
                "length", {"prompt_tokens": 13, "completion_tokens": 1116}
            )

    app = create_application(
        Settings(_env_file=None, extra_retries=retries),
        model=Truncated(),
        providers={},
        storage_root=tmp_path,
    )
    result = await app.run("规划上海行程")
    rows = [r for r in app.sessions.read_runs() if r["type"] == "model_call"]
    assert len(rows) == retries + 1 and all(r["status"] == "error" for r in rows)
    assert sum(r["input_tokens"] for r in rows) == 13 * (retries + 1)
    assert sum(r["output_tokens"] for r in rows) == 1116 * (retries + 1)
    assert all("length" in r["error"] for r in rows)
    assert result["status"] == "partial" and not result["tool_results"]


@pytest.mark.asyncio
async def test_usage_is_recorded_once_per_model_request(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    model = ScriptedModel(
        [
            SimpleNamespace(
                content=[{"type": "text", "text": "你好"}],
                usage={"input_tokens": 11, "output_tokens": 7},
            )
        ]
    )
    app = create_application(
        Settings(_env_file=None), model=model, providers={}, storage_root=tmp_path, user_id="a"
    )
    result = await app.run("你好")
    rows = app.sessions.read_runs(turn_id=result["turn_id"])
    models = [r for r in rows if r["type"] == "model_call"]
    assert len(models) == 1 and models[0]["input_tokens"] == 11 and models[0]["output_tokens"] == 7
    assert len([r for r in rows if r["type"] == "external_attempt"]) == 1


@pytest.mark.asyncio
async def test_openai_connection_error_is_retried_by_only_owner():
    import httpx
    from openai import APIConnectionError
    from support.native import FakeClock

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.resilience import call_with_retry
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.settings import Settings

    settings = Settings(_env_file=None)
    clock = FakeClock()
    scheduler = Scheduler(settings, clock=clock)
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) < 3:
            raise APIConnectionError(request=httpx.Request("GET", "https://example.com"))
        return "ok"

    assert (
        await call_with_retry(
            fetch,
            endpoint="llm",
            budget=TurnBudget(settings, clock=clock),
            scheduler=scheduler,
            clock=clock,
        )
        == "ok"
    )
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_cache_result_has_own_record_and_no_extra_attempt(tmp_path):
    from datetime import datetime

    from support.native import NativeRuntimeFixture

    from trip_assistant.providers.contracts import AgentDataResult, Source

    class Provider:
        hotel_places = True

        async def search(self, q):
            src = Source("fixture", datetime.now(UTC), "https://example.com")
            return AgentDataResult("ok", q.to_dict(), [], [], src, src.fetched_at, None)

    rt = NativeRuntimeFixture(tmp_path, {"hotel": Provider()})
    events = []
    rt.scheduler.record = events.append
    from trip_assistant.runtime.limits import TurnBudget

    ctx = {"turn_id": "t", "budget": TurnBudget(rt.settings)}
    item = {"domain": "hotel", "parameters": {"city": "上海"}}
    await rt.queries.fetch(item, ctx)
    await rt.queries.fetch(item, ctx)
    assert len([e for e in events if e["type"] == "external_attempt"]) == 1
    queries = [e for e in events if e["type"] == "query_result"]
    assert [e["cache_hit"] for e in queries] == [False, True]
    assert all("items" not in e and "parameters" not in e for e in queries)
