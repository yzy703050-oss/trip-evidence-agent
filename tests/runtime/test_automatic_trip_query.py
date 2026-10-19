import json

import pytest
from support.native import NativeRuntimeFixture
from test_batched_intents import select_and_reply, single_trip
from test_lookup_planning import batch

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.limits import TurnBudget


@pytest.mark.asyncio
async def test_only_write_call_dispatches_queries_before_review_and_saves_in_two_calls(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    runtime.model.script = [
        batch(("p", "write_trip", single_trip())),
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    result = await runtime.run("重庆去上海单程")
    assert result["status"] == "completed" and len(runtime.model.messages) == 2
    assert [r["name"] for r in result["tool_results"]] == ["write_trip", "write_trip"]
    assert len(result["execution_jobs"]) == 1 and len(train.calls) == 1
    calls = [
        c for e in runtime.sessions.read_events(event_type="tool_call") for c in e["tool_calls"]
    ]
    assert all(c["name"] != "query" for c in calls)
    assert not runtime.sessions.pending_tool_calls()


def test_public_query_accepts_only_independent_plan(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    with pytest.raises(ValueError):
        runtime.registry["query"].validate({"request_kind": "planning", "trip_ref": "$batch"})
    schema = runtime.registry["query"].schema["function"]["parameters"]
    assert "trip_ref" not in schema["properties"]
    assert (
        "plan_ref"
        not in runtime.registry["write_trip"].schema["function"]["parameters"]["properties"]
    )


@pytest.mark.asyncio
async def test_write_service_still_does_not_query_suppliers(tmp_path):
    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await runtime.trip_service.apply(
        single_trip()["tripplan"],
        {
            "operation_id": "p",
            "budget": TurnBudget(runtime.settings),
            "original_query": "重庆去上海单程",
        },
    )
    assert written["stage"] == "awaiting_query" and not train.calls


@pytest.mark.asyncio
async def test_mixed_independent_and_trip_queries_share_supplier_cache(tmp_path):
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120), "hotel": hotel}
    )
    proposal = single_trip()
    task = proposal["tripplan"]["tasks"][0]
    task.update(requires_hotel=True)
    task["conditions"]["nights"] = 1
    task["items"].append({"domain": "hotel", "parameters": {"keywords": "如家"}})

    def choose(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["query_results"][0]["data"][0]["candidates_in_tripplan"]
        chosen = select_and_reply(payload)
        view = payload["tripplan"]["candidates"][1]
        chosen.content[0]["input"]["tripplan"]["tasks"][0]["selection_refs"]["hotel"] = {
            "query_id": view["query_id"],
            "result_revision": view["result_revision"],
            "candidate_id": view["items"][0]["id"],
        }
        return chosen

    runtime.model.script = [
        batch(
            ("p", "write_trip", proposal),
            (
                "independent",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [
                        {"domain": "hotel", "parameters": {"city": "上海", "keywords": "如家"}}
                    ],
                },
            ),
        ),
        choose,
    ]
    result = await runtime.run("查上海如家酒店，规划重庆去上海单程住一晚")
    assert result["status"] == "completed" and len(runtime.model.messages) == 2
    assert len(hotel.calls) == 1 and len(result["execution_jobs"]) == 1


@pytest.mark.asyncio
async def test_internal_queries_are_in_whole_batch_budget_before_any_write(tmp_path):
    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    proposal = single_trip()
    proposal["tripplan"]["tasks"][0]["items"] = [
        {"domain": "train", "parameters": {"departure_date": f"2026-10-{day:02d}"}}
        for day in range(1, 14)
    ]
    runtime.model.script = [batch(("p", "write_trip", proposal)), "本次查询量超限，未执行。"]
    result = await runtime.run("重庆去上海单程")
    assert result["tool_results"][0]["error"]["code"] == "EXTERNAL_QUERY_LIMIT_EXCEEDED"
    assert not train.calls and runtime.trip_service.store.list_all() == []


async def saved_write(runtime):
    proposal = single_trip()
    turn = {
        "turn_id": "t",
        "operation_id": "t:p",
        "session_id": "s",
        "original_query": "重庆去上海单程",
        "budget": TurnBudget(runtime.settings),
    }
    runtime.sessions.record_call({"id": "p", "name": "write_trip", "arguments": proposal}, "t")
    runtime.sessions.checkpoint_turn(
        "t",
        {
            "query": turn["original_query"],
            "planning_rounds_used": 1,
            "planning_started": True,
            "executions": {"p": {"state": "started", "operation_id": "t:p"}},
        },
    )
    written = await runtime.trip_service.apply(proposal["tripplan"], turn)
    return turn, written


@pytest.mark.asyncio
async def test_resume_saved_write_queues_queries_and_executes_before_next_model(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn, written = await saved_write(runtime)
    restored = restore_turn(
        runtime.sessions, "t", settings=runtime.settings, trip_service=runtime.trip_service
    )
    assert restored["can_resume"]
    from trip_assistant.runtime.actions import query_jobs

    assert len(query_jobs(runtime.sessions, "t")) == 1
    assert not train.calls
    runtime.model.script = [lambda messages: select_and_reply(json.loads(messages[-1]["content"]))]
    result = await runtime.run(
        turn["original_query"], context=restored["context"], budget=restored["budget"]
    )
    assert result["status"] == "completed" and len(runtime.model.messages) == 1
    assert len(train.calls) == 1 and result["budget"]["planning_rounds_used"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("has_receipt", [True, False])
@pytest.mark.parametrize("drop_checkpoint", [True, False])
async def test_running_job_recovery_uses_receipt_or_stops_without_replay(
    tmp_path, has_receipt, drop_checkpoint
):
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.trip_jobs import enqueue_trip_query, record_job

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn, written = await saved_write(runtime)
    runtime.sessions.record_result("p", {"call_id": "p", "name": "write_trip", **written}, "t")
    enqueue_trip_query(runtime.sessions, turn, "p", written)
    from trip_assistant.runtime.actions import query_jobs

    job = next(iter(query_jobs(runtime.sessions, "t").values()))
    record_job(runtime.sessions, turn, job, "running")
    if has_receipt:
        await runtime.trip_queries.run(
            written["trip_plan_id"],
            written["revision"],
            {**turn, "operation_id": job["operation_id"]},
        )
    calls = len(train.calls)
    if drop_checkpoint:
        runtime.sessions.checkpoint_turn("t", {"trip_query_jobs": {}})
    restored = restore_turn(
        runtime.sessions, "t", settings=runtime.settings, trip_service=runtime.trip_service
    )
    assert restored["can_resume"] is has_receipt
    recovered = next(iter(query_jobs(runtime.sessions, "t").values()))
    assert recovered["state"] == ("completed" if has_receipt else "unknown")
    assert len(train.calls) == calls
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_unknown_job_stays_nonresumable_on_repeated_restore(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.trip_jobs import enqueue_trip_query, record_job

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    turn, written = await saved_write(runtime)
    runtime.sessions.record_result("p", {"call_id": "p", "name": "write_trip", **written}, "t")
    enqueue_trip_query(runtime.sessions, turn, "p", written)
    from trip_assistant.runtime.actions import query_jobs

    record_job(
        runtime.sessions, turn, next(iter(query_jobs(runtime.sessions, "t").values())), "running"
    )
    for _ in range(2):
        result = restore_turn(
            runtime.sessions, "t", settings=runtime.settings, trip_service=runtime.trip_service
        )
        assert not result["can_resume"] and result["unknown_calls"]


@pytest.mark.asyncio
async def test_failed_auto_query_preserves_commit_and_does_not_loop_itself(tmp_path):
    class Train(SimulatedTrainProvider):
        fail = False
        failures = 0

        async def search(self, query):
            if self.fail:
                self.failures += 1
                raise RuntimeError("simulated permanent failure")
            return await super().search(query)

    train = Train(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    runtime.model.script = [
        batch(("p", "write_trip", single_trip())),
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    initial = await runtime.run("重庆去上海单程")
    original = runtime.trip_service.store.load(initial["trip"]["trip_plan_id"])
    train.fail = True
    runtime.model.messages.clear()
    runtime.model.script = [
        batch(
            (
                "refresh",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": original["id"],
                        "expected_revision": original["revision"],
                        "tasks": [
                            {
                                "task_id": original["tasks"][0]["id"],
                                "items": [{"domain": "train", "refresh": True}],
                            }
                        ],
                    },
                },
            )
        ),
        "车次刷新失败，原已保存方案保留，没有取得最新报价。",
    ]
    result = await runtime.run("刷新这个行程的车次")
    saved = runtime.trip_service.store.load(original["id"])
    assert result["status"] == "partial" and saved["committed"] == original["committed"]
    assert train.failures == 1 and len(result["execution_jobs"]) == 1
    assert (
        len(runtime.model.messages) == 2
        and result["execution_jobs"][0]["data"][0]["status"] == "error"
    )


@pytest.mark.asyncio
async def test_activity_only_amend_does_not_dispatch_another_query(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    runtime.model.script = [
        batch(("p", "write_trip", single_trip())),
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    initial = await runtime.run("重庆去上海单程")
    original = runtime.trip_service.store.load(initial["trip"]["trip_plan_id"])
    count = len(train.calls)
    runtime.model.script = [
        batch(
            (
                "activity",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "final_answer": "下午安排沿江散步。",
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": original["id"],
                        "expected_revision": original["revision"],
                        "tasks": [
                            {
                                "task_id": original["tasks"][0]["id"],
                                "components": ["content"],
                                "content": [
                                    {
                                        "purpose": "沿江散步",
                                        "description": "抵达后下午沿江散步",
                                        "date": "2026-10-17",
                                        "time": "下午",
                                        "source": "proposal",
                                        "strength": "soft",
                                    }
                                ],
                            }
                        ],
                    },
                },
            )
        )
    ]
    result = await runtime.run("加一项下午沿江散步，交通不变")
    assert result["status"] == "completed" and result["execution_jobs"] == []
    assert len(train.calls) == count and result["final_answer"] == "下午安排沿江散步。"


@pytest.mark.asyncio
async def test_independent_and_automatic_queries_share_one_preflight_limit(tmp_path):
    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    lookup = {
        "request_kind": "planning",
        "query_plan": [
            {
                "domain": "train",
                "parameters": {
                    "origin": "北京",
                    "destination": "上海",
                    "departure_date": f"2026-10-{day:02d}",
                },
            }
            for day in range(1, 13)
        ],
    }
    runtime.model.script = [
        batch(("p", "write_trip", single_trip()), ("lookup", "query", lookup)),
        "查询量超限。",
    ]
    result = await runtime.run("规划重庆到上海，并查询北京到上海这些日期的车次")
    assert all(
        r["error"]["code"] == "EXTERNAL_QUERY_LIMIT_EXCEEDED" for r in result["tool_results"]
    )
    assert not train.calls and not runtime.trip_service.store.list_all()


@pytest.mark.asyncio
async def test_existing_completed_query_receipt_restores_without_new_auto_job(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn, written = await saved_write(runtime)
    # Historical sessions may contain the old explicit query call; restore only its receipt.
    runtime.sessions.record_result("p", {"call_id": "p", "name": "write_trip", **written}, "t")
    runtime.sessions.record_call(
        {
            "id": "q",
            "name": "query",
            "arguments": {
                "trip_ref": written["trip_plan_id"],
                "expected_revision": written["revision"],
            },
        },
        "t",
    )
    await runtime.trip_queries.run(
        written["trip_plan_id"], written["revision"], {**turn, "operation_id": "t:q"}
    )
    restored = restore_turn(
        runtime.sessions, "t", settings=runtime.settings, trip_service=runtime.trip_service
    )
    assert restored["can_resume"] and not restored["context"].get("trip_query_jobs")
    assert not runtime.sessions.pending_tool_calls()
    result = runtime.sessions.read_events(event_type="tool_result")[-1]["content"]
    assert result["name"] == "query" and result.get("kind") != "execution_job"


@pytest.mark.asyncio
async def test_amending_hotel_does_not_silently_retry_failed_train_item(tmp_path):
    class FailedTrain:
        calls = 0

        async def search(self, query):
            self.calls += 1
            raise RuntimeError("permanent simulation failure")

    train, hotel = FailedTrain(), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    proposal = single_trip()
    task = proposal["tripplan"]["tasks"][0]
    task.update(requires_hotel=True)
    task["conditions"]["nights"] = 1
    task["items"].append({"domain": "hotel"})

    def amend_hotel(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return batch(
            (
                "hotel",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": trip["trip_plan_id"],
                        "expected_revision": trip["revision"],
                        "tasks": [
                            {
                                "task_id": trip["tasks"][0]["id"],
                                "components": ["hotel"],
                                "items": [{"domain": "hotel", "refresh": True}],
                            }
                        ],
                    },
                },
            )
        )

    def summary(messages):
        data = json.loads(messages[-1]["content"])
        failure = data["execution_results"][0]["query_outcomes"][0]
        assert failure["domain"] == "train" and failure["status"] == "error" and failure["reason"]
        return "酒店已刷新，交通仍未取得结果。"

    runtime.model.script = [batch(("p", "write_trip", proposal)), amend_hotel, summary]
    result = await runtime.run("重庆去上海单程住一晚")
    assert train.calls == 1 and len(hotel.calls) == 2
    assert result["stop_reason"] is None
    assert [v["domain"] for v in result["execution_jobs"][-1]["data"]] == ["hotel"]
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert any(
        p["reason"] == "query_failed" and p["item"]["domain"] == "train"
        for p in saved["pending_queries"]
    )
