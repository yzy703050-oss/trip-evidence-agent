import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from support.native import NativeRuntimeFixture, write_then_query
from test_batched_intents import select_and_reply, single_trip

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget


async def seed(runtime, *, hotel=False):
    proposal = single_trip()["tripplan"]
    if hotel:
        task = proposal["tasks"][0]
        task.update(requires_hotel=True)
        task["conditions"]["nights"] = 1
        task["items"].append({"domain": "hotel"})
    return await write_then_query(
        runtime.trip_service,
        proposal,
        {
            "operation_id": "seed",
            "budget": TurnBudget(runtime.settings),
            "original_query": "重庆去上海单程",
            "session_id": "s",
        },
    )


def expire_on_select(runtime):
    original = runtime.trip_service._select
    injected = []

    def expire(work, task, domain, ref, turn):
        if not injected:
            injected.append(True)
            work["results_by_query"][ref["query_id"]]["fetched_at"] = (
                datetime.now(UTC) - timedelta(days=1)
            ).isoformat()
        return original(work, task, domain, ref, turn)

    runtime.trip_service._select = expire


@pytest.mark.asyncio
async def test_expiry_is_refreshed_by_harness_without_model_amend(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    saved = await seed(runtime)
    expire_on_select(runtime)
    versions = []

    def choose(messages):
        payload = json.loads(messages[-1]["content"])
        view = payload["tripplan"]["candidates"][0]
        versions.append(view["result_revision"])
        assert "EVIDENCE_EXPIRED" not in json.dumps(messages)
        if len(versions) == 2:
            assert payload["previous_round_summary"]["results"][0]["selection_saved"] is False
            assert payload["adjustment_history"][0]["effect"] == "not_applied"
        choice = select_and_reply(payload)
        choice.content[0]["id"] = "select-" + str(len(versions))
        return choice

    runtime.model.script = [choose, choose]
    result = await runtime.run(
        "从这次行程选车并保存", context={"review_trip_plan_id": saved["trip_plan_id"]}
    )
    assert result["status"] == "completed", (result, versions)
    assert len(runtime.model.messages) == 2
    assert versions == [1, 2] and len(train.calls) == 2
    assert result["tool_results"][0]["selection_saved"] is False
    calls = [
        c for e in runtime.sessions.read_events(event_type="tool_call") for c in e["tool_calls"]
    ]
    assert [c["arguments"]["tripplan"]["operation"] for c in calls] == ["select", "select"]
    assert any(
        e["content"]["error"]["code"] == "EVIDENCE_EXPIRED"
        for e in runtime.sessions.read_events(event_type="execution_check")
    )
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_refresh_executor_preserves_original_conditions_and_other_candidates(tmp_path):
    train, hotel = SimulatedTrainProvider(duration_minutes=120), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    result = await seed(runtime, hotel=True)
    saved = runtime.trip_service.store.load(result["trip_plan_id"])
    binding = next(r for r in saved["results_by_query"].values() if r["domain"] == "train")
    plan = runtime.trip_queries.prepare_refresh(saved["id"], saved["revision"], [binding["id"]])
    queried = await runtime.trip_queries.refresh(
        plan,
        {
            "operation_id": "refresh",
            "budget": TurnBudget(runtime.settings),
            "original_query": "更新候选",
        },
    )
    after = runtime.trip_service.store.load(saved["id"])
    for key in ("conditions", "field_sources", "condition_strengths", "content"):
        assert after["tasks"][0][key] == saved["tasks"][0][key]
    assert after.get("committed") == saved.get("committed")
    assert {v["domain"] for v in queried["trip"]["candidates"]} == {"train", "hotel"}
    assert {v["domain"] for v in queried["data"]} == {"train"}
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    assert train.calls[0] == train.calls[1]
    assert after["results_by_query"][binding["id"]]["result_revision"] == 2


def test_external_query_reservation_survives_resume_and_counts_internal_refresh(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    budget = TurnBudget(runtime.settings)
    budget.check_external_queries([str(i) for i in range(12)])
    restored = TurnBudget(runtime.settings, snapshot=budget.checkpoint())
    with pytest.raises(Exception, match="external_query_limit_exceeded"):
        restored.check_external_queries(["new-refresh"])


@pytest.mark.asyncio
@pytest.mark.parametrize("planning", [False, True])
async def test_pre_review_refresh_requires_confirmed_planning(tmp_path, planning):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    saved = runtime.trip_service.store.load(written["trip_plan_id"])
    for binding in saved["results_by_query"].values():
        binding["fetched_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    saved = runtime.trip_service.store.save(saved, expected_revision=saved["revision"])
    before = deepcopy(saved)

    def respond(messages):
        payload = json.loads(messages[-1]["content"])
        if not planning:
            return "这是此前保存的历史候选，本次没有刷新。"
        assert payload["tripplan"]["candidates"][0]["result_revision"] == 2
        return select_and_reply(payload)

    runtime.model.script = [respond]
    budget = TurnBudget(runtime.settings, snapshot={"planning_started": planning})
    result = await runtime.run(
        "查看已有行程", budget=budget, context={"review_trip_plan_id": saved["id"]}
    )
    assert len(runtime.model.messages) == 1 and len(train.calls) == 1 + int(planning)
    if planning:
        assert result["status"] == "completed"
    else:
        assert runtime.trip_service.store.load(saved["id"]) == before
        assert result["execution_jobs"] == []


@pytest.mark.asyncio
async def test_mixed_batch_includes_refresh_in_external_limit_before_any_effect(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    from trip_assistant.memory.planning import review_payload

    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "选择"}, written["trip_plan_id"]
    )
    choice = select_and_reply(payload).content[0]
    expire_on_select(runtime)
    calls = [
        {"id": "s", "name": "write_trip", "arguments": choice["input"]},
        {
            "id": "q",
            "name": "query",
            "arguments": {
                "request_kind": "planning",
                "query_plan": [
                    {
                        "domain": "train",
                        "parameters": {
                            "origin": "重庆",
                            "destination": "上海",
                            "departure_date": f"2026-11-{day:02}",
                        },
                    }
                    for day in range(1, 13)
                ],
            },
        },
        {
            "id": "m",
            "name": "write_memory",
            "arguments": {
                "request_kind": "planning",
                "updates": [{"op": "add", "text": "喜欢安静"}],
            },
        },
    ]
    before = deepcopy(runtime.memory.data)
    result = await execute_batch(
        calls,
        registry=runtime.registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )
    assert all(r["status"] == "rejected" for r in result)
    assert result[0]["error"]["code"] == "EXTERNAL_QUERY_LIMIT_EXCEEDED"
    assert len(train.calls) == 1 and runtime.memory.data == before
    assert not runtime.sessions.read_events(event_type="execution_job")


@pytest.mark.asyncio
async def test_failed_automatic_refresh_preserves_committed_and_does_not_release_answer(tmp_path):
    class Train(SimulatedTrainProvider):
        fail = False

        async def search(self, query):
            if self.fail:
                raise RuntimeError("permanent simulated failure")
            return await super().search(query)

    train = Train(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    from trip_assistant.memory.planning import review_payload

    payload = review_payload(
        runtime.sessions, {"turn_id": "seed", "original_query": "选择"}, written["trip_plan_id"]
    )
    selected = select_and_reply(payload).content[0]["input"]["tripplan"]
    await runtime.trip_service.apply(selected, {"operation_id": "seed:select"})
    before = runtime.trip_service.store.load(written["trip_plan_id"])["committed"]
    current = runtime.trip_service.store.load(written["trip_plan_id"])
    await write_then_query(
        runtime.trip_service,
        {
            "operation": "amend",
            "trip_plan_id": current["id"],
            "expected_revision": current["revision"],
            "tasks": [
                {
                    "task_id": current["tasks"][0]["id"],
                    "items": [{"domain": "train", "refresh": True}],
                }
            ],
        },
        {
            "operation_id": "seed:draft",
            "budget": TurnBudget(runtime.settings),
            "original_query": "刷新车次",
            "session_id": "s",
        },
    )
    train.fail = True
    expire_on_select(runtime)
    answer = "更新车次失败，原行程记录保留，旧车次与票价目前未核实。"
    runtime.model.script = [lambda m: select_and_reply(json.loads(m[-1]["content"])), answer]
    result = await runtime.run(
        "选车并保存", context={"review_trip_plan_id": written["trip_plan_id"]}
    )
    assert result["status"] == "partial" and result["final_answer"] == answer
    assert result["execution_facts"]["saved"] is False
    assert runtime.trip_service.store.load(written["trip_plan_id"])["committed"] == before
    assert len(result["execution_jobs"]) == 1 and len(runtime.model.messages) == 2
    assert runtime.model.messages[-1][1] == {"tool_choice": "none"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unqueued", "queued", "receipt", "unknown"])
async def test_refresh_resume_reconciles_durable_jobs_without_replay(tmp_path, monkeypatch, mode):
    from trip_assistant.memory.planning import review_payload
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.trip_jobs import record_job

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "选择"}, written["trip_plan_id"]
    )
    choice = select_and_reply(payload).content[0]
    expire_on_select(runtime)
    budget = TurnBudget(runtime.settings)
    turn = {"turn_id": "t", "original_query": "选择并保存", "budget": budget}
    runtime.sessions.checkpoint_turn(
        "t",
        {
            "query": turn["original_query"],
            "status": "running",
            "execution_state": {"phase": "executing"},
        },
    )

    async def no_start(*args):
        return []

    with monkeypatch.context() as patch:
        patch.setattr("trip_assistant.runtime.execution.run_trip_jobs", no_start)
        if mode == "unqueued":
            patch.setattr("trip_assistant.runtime.execution.enqueue_trip_query", lambda *a: None)
        await execute_batch(
            [{"id": "s", "name": "write_trip", "arguments": choice["input"]}],
            registry=runtime.registry,
            turn=turn,
            budget=budget,
            sessions=runtime.sessions,
        )
    from trip_assistant.runtime.actions import query_jobs

    job = next(iter(query_jobs(runtime.sessions, "t").values()), None)
    if mode in {"receipt", "unknown"}:
        record_job(runtime.sessions, turn, job, "running")
        if mode == "receipt":
            await runtime.trip_queries.refresh(
                job["refresh"], {**turn, "operation_id": job["operation_id"]}
            )
    runtime.sessions.checkpoint_turn("t", {"trip_query_jobs": {}})
    restored = restore_turn(
        runtime.sessions, "t", settings=runtime.settings, trip_service=runtime.trip_service
    )
    assert restored["can_resume"] is (mode != "unknown")
    count = len(train.calls)
    if mode == "unknown":
        assert count == 1 and restored["unknown_calls"]
        return
    runtime.model.script = [lambda m: select_and_reply(json.loads(m[-1]["content"]))]
    result = await runtime.run(
        turn["original_query"], context=restored["context"], budget=restored["budget"]
    )
    assert result["status"] == "completed" and len(runtime.model.messages) == 1
    assert len(train.calls) == 2
    if mode == "receipt":
        assert count == len(train.calls)


@pytest.mark.asyncio
async def test_expiry_during_actual_write_is_also_recovered(tmp_path):
    from dataclasses import replace

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    tool = runtime.registry["write_trip"]
    attempts = []

    async def write(args, turn):
        if not attempts:
            attempts.append(True)
            runtime.scheduler.settings = runtime.settings.model_copy(
                update={"evidence_max_age_seconds": -1}
            )
            try:
                return await tool.handler(args, turn)
            finally:
                runtime.scheduler.settings = runtime.settings
        return await tool.handler(args, turn)

    runtime.registry["write_trip"] = replace(tool, handler=write)

    def choose(messages):
        payload = json.loads(messages[-1]["content"])
        assert "EVIDENCE_EXPIRED" not in json.dumps(messages)
        response = select_and_reply(payload)
        response.content[0]["id"] = "select-" + str(len(runtime.model.messages))
        return response

    runtime.model.script = [choose, choose]
    result = await runtime.run("选车保存", context={"review_trip_plan_id": written["trip_plan_id"]})
    assert result["status"] == "completed" and len(runtime.model.messages) == 2
    assert len(train.calls) == 2 and result["tool_results"][0]["selection_saved"] is False


@pytest.mark.asyncio
async def test_last_planning_interaction_does_not_refresh_without_selection_budget(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    expire_on_select(runtime)
    runtime.model.script = [
        lambda m: select_and_reply(json.loads(m[-1]["content"])),
        "本轮未保存选择，旧方案保留。",
    ]
    budget = TurnBudget(
        runtime.settings, snapshot={"planning_rounds_used": runtime.settings.planning_rounds - 1}
    )
    result = await runtime.run(
        "选择", budget=budget, context={"review_trip_plan_id": written["trip_plan_id"]}
    )
    assert len(train.calls) == 1 and not result["execution_jobs"]
    assert result["stop_reason"] == "planning_round_limit_exceeded"


@pytest.mark.asyncio
async def test_second_expiry_stops_after_one_automatic_refresh(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    runtime.scheduler.settings = runtime.settings.model_copy(
        update={"evidence_max_age_seconds": -1}
    )

    def choose(messages):
        response = select_and_reply(json.loads(messages[-1]["content"]))
        response.content[0]["id"] = "select-" + str(len(runtime.model.messages))
        return response

    answer = "这次未能核实有效车次，原有安排和进度保留。"
    runtime.model.script = [choose, choose, answer]
    result = await runtime.run("选择保存", context={"review_trip_plan_id": written["trip_plan_id"]})
    assert result["stop_reason"] == "automatic_refresh_limit"
    assert result["tool_results"][1]["error"]["code"] == "AUTOMATIC_REFRESH_LIMIT"
    assert result["final_answer"] == answer and len(result["execution_jobs"]) == 1
    assert len(train.calls) == 2 and runtime.model.messages[-1][1] == {"tool_choice": "none"}


@pytest.mark.asyncio
async def test_refresh_refuses_changed_trip_revision_before_network(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    saved = runtime.trip_service.store.load(written["trip_plan_id"])
    query_id = next(iter(saved["results_by_query"]))
    plan = runtime.trip_queries.prepare_refresh(saved["id"], saved["revision"], [query_id])
    changed = runtime.trip_service.store.save(saved, expected_revision=saved["revision"])
    with pytest.raises(ValueError, match="revision changed"):
        await runtime.trip_queries.refresh(
            plan, {"operation_id": "stale-job", "budget": TurnBudget(runtime.settings)}
        )
    assert len(train.calls) == 1 and runtime.trip_service.store.load(saved["id"]) == changed


@pytest.mark.asyncio
async def test_recoverable_expiry_does_not_drop_ready_independent_tools(tmp_path):
    from trip_assistant.memory.planning import review_payload

    train, hotel = SimulatedTrainProvider(duration_minutes=120), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    written = await seed(runtime)
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "选择"}, written["trip_plan_id"]
    )
    choice = select_and_reply(payload).content[0]
    expire_on_select(runtime)
    calls = [
        {"id": "s", "name": "write_trip", "arguments": choice["input"]},
        {
            "id": "q",
            "name": "query",
            "arguments": {
                "request_kind": "planning",
                "query_plan": [
                    {"domain": "hotel", "parameters": {"city": "上海", "keywords": "如家"}}
                ],
            },
        },
        {
            "id": "m",
            "name": "write_memory",
            "arguments": {
                "request_kind": "planning",
                "updates": [{"op": "add", "text": "喜欢安静"}],
            },
        },
    ]
    results = await execute_batch(
        calls,
        registry=runtime.registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )
    assert [r["status"] for r in results[:3]] == ["partial", "ok", "ok"]
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    assert any(m["text"] == "喜欢安静" for m in runtime.memory.data["travel_memories"])


@pytest.mark.asyncio
async def test_auto_refresh_reuses_fresh_evidence_from_same_batch_independent_query(tmp_path):
    from trip_assistant.memory.planning import review_payload

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    written = await seed(runtime)
    saved = runtime.trip_service.store.load(written["trip_plan_id"])
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "选择"}, saved["id"]
    )
    choice = select_and_reply(payload).content[0]
    binding = next(iter(saved["results_by_query"].values()))
    expire_on_select(runtime)
    calls = [
        {"id": "s", "name": "write_trip", "arguments": choice["input"]},
        {
            "id": "q",
            "name": "query",
            "arguments": {
                "request_kind": "planning",
                "query_plan": [
                    {"domain": "train", "parameters": binding["parameters"], "refresh": True}
                ],
            },
        },
    ]
    results = await execute_batch(
        calls,
        registry=runtime.registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )
    assert len(train.calls) == 2
    assert results[-1]["data"][0]["cache_hit"] is True
    assert results[-1]["data"][0]["result_revision"] == 2
