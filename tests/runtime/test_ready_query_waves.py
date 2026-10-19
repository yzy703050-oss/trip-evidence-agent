import asyncio

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.settings import Settings


def plan(identity, origin="重庆"):
    return {
        "id": identity,
        "name": "write_trip",
        "arguments": {
            "request_kind": "planning",
            "tripplan": {
                "default_round_trip": False,
                "tasks": [
                    {
                        "origin": origin,
                        "destination": "上海",
                        "conditions": {"departure_date": "2026-10-19"},
                        "items": [{"domain": "train"}],
                    }
                ],
            },
        },
    }


async def execute(runtime, calls):
    return await execute_batch(
        calls,
        registry=runtime.registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )


async def test_independent_query_overlaps_automatic_trip_query(tmp_path):
    train_started, hotel_started = asyncio.Event(), asyncio.Event()

    class Train(SimulatedTrainProvider):
        async def search(self, query):
            train_started.set()
            await asyncio.wait_for(hotel_started.wait(), 0.5)
            return await super().search(query)

    class Hotel(SimulatedHotelProvider):
        async def search(self, query):
            hotel_started.set()
            await asyncio.wait_for(train_started.wait(), 0.5)
            return await super().search(query)

    runtime = NativeRuntimeFixture(
        tmp_path,
        {"train": Train(), "hotel": Hotel()},
        settings=Settings(_env_file=None, extra_retries=0),
    )
    lookup = {
        "id": "q",
        "name": "query",
        "arguments": {
            "request_kind": "planning",
            "query_plan": [{"domain": "hotel", "parameters": {"city": "北京"}}],
        },
    }
    results = await execute(runtime, [lookup, plan("p")])
    assert results[0]["status"] == "ok", results
    assert results[-1]["name"] == "trip_query" and results[-1]["status"] == "ok"
    assert [r["call_id"] for r in results[:2]] == ["q", "p"]
    assert not runtime.sessions.pending_tool_calls()


async def test_different_trip_query_jobs_overlap_and_keep_task_identity(tmp_path):
    all_started = asyncio.Event()
    origins = set()

    class Train(SimulatedTrainProvider):
        async def search(self, query):
            origins.add(query.origin)
            if len(origins) == 2:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), 0.5)
            return await super().search(query)

    runtime = NativeRuntimeFixture(
        tmp_path, {"train": Train()}, settings=Settings(_env_file=None, extra_retries=0)
    )
    results = await execute(runtime, [plan("a"), plan("b", "北京")])
    jobs = [r for r in results if r["name"] == "trip_query"]
    assert len(jobs) == 2 and all(r["status"] == "ok" for r in jobs), jobs
    assert len({r["trip_plan_id"] for r in jobs}) == 2
    for job in jobs:
        saved = runtime.memory.workflow_store.load(job["trip_plan_id"])
        assert saved["tasks"][0]["origin"] in origins
        assert all(
            row["task_id"] == saved["tasks"][0]["id"] for row in saved["results_by_query"].values()
        )


async def test_same_trip_jobs_remain_sequential(tmp_path):
    from trip_assistant.runtime.trip_jobs import enqueue_trip_query, run_trip_jobs

    runtime = NativeRuntimeFixture(tmp_path)
    turn = {"turn_id": "t", "budget": TurnBudget(runtime.settings)}
    for parent in ["a", "b"]:
        enqueue_trip_query(
            runtime.sessions,
            turn,
            parent,
            {
                "trip_plan_id": "same",
                "revision": 1,
                "pending_queries": [{"reason": "awaiting_query"}],
            },
        )

    class Executor:
        active = 0

        async def run(self, trip_id, revision, context):
            self.active += 1
            assert self.active == 1
            await asyncio.sleep(0)
            self.active -= 1
            return {"name": "trip_query", "kind": "execution_job", "status": "ok"}

    assert len(await run_trip_jobs(Executor(), turn, runtime.sessions)) == 2


async def test_failed_dispatch_cancels_and_joins_independent_query(tmp_path, monkeypatch):
    from dataclasses import replace

    from trip_assistant.runtime import execution

    runtime = NativeRuntimeFixture(tmp_path, {"hotel": SimulatedHotelProvider()})
    started, stopped = asyncio.Event(), asyncio.Event()

    async def query(args, turn):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def failed_jobs(*args):
        await started.wait()
        raise RuntimeError("dispatch_failed")

    runtime.registry["query"] = replace(runtime.registry["query"], handler=query)
    monkeypatch.setattr(execution, "run_trip_jobs", failed_jobs)
    lookup = {
        "id": "q",
        "name": "query",
        "arguments": {
            "request_kind": "planning",
            "query_plan": [{"domain": "hotel", "parameters": {"city": "北京"}}],
        },
    }
    try:
        with pytest.raises(RuntimeError, match="dispatch_failed"):
            await execute(runtime, [plan("p"), lookup])
        assert stopped.is_set(), "batch returned while its query was still executing"
    finally:
        # The runtime must own cancellation; old implementations leave this pending at teardown.
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and task.get_coro().__name__ == "execute"
        ]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
