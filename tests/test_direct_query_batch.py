import asyncio
from datetime import UTC, datetime

import pytest

from agents.contracts import RunState
from agents.workflow_contracts import create_workflow
from travel_data.contracts import AgentDataResult, Source
from travel_data.tools import ToolExecutor


@pytest.mark.asyncio
async def test_independent_weather_parallel_scope_and_budget():
    from travel_data.batch_execution import execute_query_batch

    started = set()
    event = asyncio.Event()

    class Public:
        async def weather(self, city, date):
            started.add(city)
            if len(started) == 2:
                event.set()
            await asyncio.wait_for(event.wait(), 0.5)
            return AgentDataResult(
                "ok",
                {"city": city, "date": date},
                [{"date": date, "temp_c": 22}],
                [],
                Source("weather", datetime.now(UTC), "https://example.com/weather"),
                datetime.now(UTC),
                None,
            )

    run = RunState("t")
    run.workflow_tool_budget = 2
    batch = {
        "requests": [
            {
                "request_id": "a",
                "name": "weather_query",
                "parameters": {"city": "上海", "date": None},
            },
            {
                "request_id": "b",
                "name": "weather_query",
                "parameters": {"city": "杭州", "date": None},
            },
        ]
    }
    result = await execute_query_batch(batch, run, ToolExecutor({}, Public()))
    assert [r["request_id"] for r in result["results"]] == ["a", "b"]
    assert all(r["status"] == "ok" for r in result["results"]), result
    assert run.external_request_count == 2 and run.workflow_tool_budget == 0
    assert run.current_task_id is None


def test_bind_uses_program_task_ids_and_rejects_missing_dependency():
    from agents.workflow_batches import bind_query_plan, initialize_query_containers

    w = create_workflow(
        {"tasks": [{"origin": "重庆", "destination": "上海", "requires_hotel": True}]}, {}
    )
    initialize_query_containers(w)
    assert set(w["tasks"][0]["query_containers"]) == {"train", "hotel"}
    batch = bind_query_plan(
        [
            {
                "task_index": 0,
                "domain": "train",
                "parameters": {
                    "origin": "重庆",
                    "destination": "上海",
                    "departure_date": "2026-11-03",
                    "passengers": 1,
                },
            }
        ],
        w,
        {},
    )
    assert batch["requests"][0]["task_id"] == w["tasks"][0]["id"]
    assert batch["requests"][0]["task_revision"] == 1
    with pytest.raises(ValueError):
        bind_query_plan(
            [{"task_index": 0, "domain": "train", "parameters": {"origin": "北京"}}], w, {}
        )


@pytest.mark.asyncio
async def test_train_hotel_weather_barrier_and_partial_timeout_preserves_completed():
    from travel_data.batch_execution import execute_query_batch

    started = set()
    event = asyncio.Event()

    class Executor:
        async def execute(self, name, parameters, private, call_id):
            started.add(name)
            if len(started) == 3:
                event.set()
            await asyncio.wait_for(event.wait(), 0.5)
            if name == "weather_query":
                await asyncio.Event().wait()
            return {"status": "ok", "items": [], "query": parameters}

    run = RunState("t")
    run.workflow_tool_budget = 5
    batch = {
        "requests": [
            {"request_id": d, "name": n, "parameters": {}}
            for d, n in [
                ("train", "train_search"),
                ("hotel", "hotel_search"),
                ("weather", "weather_query"),
            ]
        ]
    }
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(execute_query_batch(batch, run, Executor()), 0.1)
    assert started == {"train_search", "hotel_search", "weather_query"}
    assert set(run.domain_results) == {"train", "hotel"}
    records = {r["request_id"]: r for r in run.query_execution_records}
    assert records["hotel"]["completed_at"] and records["train"]["completed_at"]
    assert records["weather"]["started_at"] and records["weather"]["completed_at"] is None


@pytest.mark.asyncio
async def test_explicit_query_dependency_waits_and_failure_blocks_only_dependent():
    from travel_data.batch_execution import execute_query_batch

    calls = []

    class Executor:
        async def execute(self, name, parameters, private, call_id):
            if call_id == "hotel":
                assert "train" in calls
            calls.append(call_id)
            return {"status": "error" if call_id == "train" else "ok", "items": []}

    run = RunState("t")
    batch = {
        "requests": [
            {
                "request_id": "hotel",
                "name": "hotel_search",
                "parameters": {},
                "depends_on": ["train"],
            },
            {"request_id": "train", "name": "train_search", "parameters": {}},
            {"request_id": "weather", "name": "weather_query", "parameters": {}},
        ]
    }
    result = await execute_query_batch(batch, run, Executor())
    assert set(calls) == {"train", "weather"}
    assert result["gaps"] == [{"request_id": "hotel", "reason": "dependency_unavailable"}]


@pytest.mark.asyncio
async def test_resume_completed_query_satisfies_dependency_without_replay():
    from travel_data.batch_execution import execute_query_batch

    calls = []

    class Executor:
        async def execute(self, name, parameters, private, call_id):
            calls.append(call_id)
            return {"status": "ok", "items": []}

    run = RunState("t")
    run.query_execution_records = [
        {"request_id": "train", "started_at": "then", "completed_at": "done", "result_status": "ok"}
    ]
    result = await execute_query_batch(
        {
            "requests": [
                {"request_id": "train", "name": "train_search", "parameters": {}},
                {
                    "request_id": "hotel",
                    "name": "hotel_search",
                    "parameters": {},
                    "depends_on": ["train"],
                },
            ]
        },
        run,
        Executor(),
    )
    assert calls == ["hotel"]
    assert [r["request_id"] for r in result["results"]] == ["hotel"]


def test_unknown_arrival_does_not_create_hotel_quote_dates():
    from types import SimpleNamespace

    from agents.workflow_runner import WorkflowRunner

    runner = object.__new__(WorkflowRunner)
    runner.w = create_workflow(
        {
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "requires_hotel": True,
                    "conditions": {"departure_date": "2026-11-03", "nights": 2},
                }
            ]
        },
        {},
    )
    runner.context = {}
    runner.executor = SimpleNamespace(
        providers={"hotel_search": SimpleNamespace(hotel_places=False)}
    )
    requests = runner._default_queries()
    assert [q["domain"] for q in requests] == ["train"]
    assert not runner.w["tasks"][0]["conditions"].get("check_in")
