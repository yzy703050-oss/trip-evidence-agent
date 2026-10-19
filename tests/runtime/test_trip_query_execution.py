import json

import pytest
from support.native import NativeRuntimeFixture
from test_batched_intents import select_and_reply, single_trip
from test_lookup_planning import batch

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.limits import TurnBudget


@pytest.mark.asyncio
async def test_partial_source_quality_still_uses_review_and_completes(tmp_path):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from evals.api_shaped_simulation import ApiShapedTrainProvider
    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

    runtime = NativeRuntimeFixture(tmp_path, {"train": ApiShapedTrainProvider()})
    # Keep the proposal inside the real parser's query window as calendar days pass.
    proposal = single_trip()
    proposal["tripplan"]["tasks"][0]["conditions"]["departure_date"] = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)
    ).isoformat()

    def choose(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        assert payload["tripplan"]["candidates"][0]["data_quality"] == "partial"
        return select_and_reply(payload)

    runtime.model.script = [batch(("p", "write_trip", proposal)), choose]
    result = await runtime.run("重庆去上海单程")
    assert result["status"] == "completed" and result["execution_facts"]["saved"]


@pytest.mark.asyncio
async def test_allow_repair_false_does_not_stop_query_or_awaiting_selection(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    proposal = single_trip()
    proposal["tripplan"]["allow_repair"] = False
    runtime.model.script = [
        batch(("p", "write_trip", proposal)),
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    result = await runtime.run("重庆去上海单程")
    assert result["status"] == "completed" and result["stop_reason"] is None


@pytest.mark.asyncio
async def test_bound_query_retry_uses_receipt_before_old_revision_check(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = {
        "operation_id": "p",
        "budget": TurnBudget(runtime.settings),
        "original_query": "重庆去上海单程",
        "session_id": "s",
    }
    written = await runtime.trip_service.apply(single_trip()["tripplan"], turn)

    first = await runtime.trip_queries.run(
        written["trip_plan_id"], written["revision"], {**turn, "operation_id": "q"}
    )
    second = await runtime.trip_queries.run(
        written["trip_plan_id"], written["revision"], {**turn, "operation_id": "q"}
    )
    assert first == second and len(train.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["paused", "cancelled"])
async def test_query_cannot_resume_stopped_plan(tmp_path, reason):
    from trip_assistant.runtime.errors import ToolFailure

    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = {
        "operation_id": "p",
        "budget": TurnBudget(runtime.settings),
        "original_query": "重庆去上海单程",
        "session_id": "s",
    }
    written = await runtime.trip_service.apply(single_trip()["tripplan"], turn)
    plan = runtime.trip_service.store.load(written["trip_plan_id"])
    plan["stop_reason"] = reason
    saved = runtime.trip_service.store.save_working(
        plan, expected_revision=plan["revision"], operation_id="stop"
    )
    with pytest.raises(ToolFailure, match="stopped"):
        await runtime.trip_queries.run(
            saved["id"], saved["revision"], {**turn, "operation_id": "q"}
        )
    assert not train.calls


@pytest.mark.asyncio
async def test_different_ranking_window_keeps_unique_candidate_and_can_select(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[0, 3, 6, 9, 12, 15])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    proposal = single_trip()
    proposal["tripplan"]["tasks"][0]["items"][0]["ranking"] = [
        {"field": "departure_at", "direction": "asc"}
    ]

    def select(messages):
        payload = json.loads(messages[-1]["content"])
        views = payload["query_results"][0]["data"]
        independent = views[0]
        assert independent["candidates_in_tripplan"] and len(independent["items"]) == 1
        assert len(independent["candidate_order"]) == 5
        unique = independent["items"][0]
        assert unique["train_number"] == "SIM006"
        chosen = select_and_reply(payload)
        ref = chosen.content[0]["input"]["tripplan"]["tasks"][0]["selection_refs"]["train"]
        ref["candidate_id"] = unique["id"]
        chosen.content[0]["input"]["final_answer"] = "选15点出发的SIM006。"
        return chosen

    runtime.model.script = [
        batch(
            ("p", "write_trip", proposal),
            (
                "q",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [
                        {
                            "domain": "train",
                            "parameters": {
                                "origin": "重庆",
                                "destination": "上海",
                                "departure_date": "2026-10-17",
                            },
                            "ranking": [{"field": "departure_at", "direction": "desc"}],
                        }
                    ],
                },
            ),
        ),
        select,
    ]
    result = await runtime.run("查下午车次并规划重庆去上海单程")
    assert result["status"] == "completed" and len(train.calls) == 1
    assert (
        result["trip"]["validation"]["reconstructed_tasks"][0]["train"]["train_number"] == "SIM006"
    )


@pytest.mark.asyncio
async def test_refresh_write_exposes_awaiting_query_without_old_current_candidates(tmp_path):
    from support.native import write_then_query

    from trip_assistant.memory.planning import review_payload

    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = {
        "operation_id": "p",
        "budget": TurnBudget(runtime.settings),
        "original_query": "重庆去上海单程",
        "session_id": "s",
    }
    seed = await write_then_query(runtime.trip_service, single_trip()["tripplan"], turn)
    refreshed = await runtime.trip_service.apply(
        {
            "operation": "amend",
            "trip_plan_id": seed["trip_plan_id"],
            "expected_revision": seed["revision"],
            "tasks": [
                {"task_id": seed["tasks"][0]["id"], "items": [{"domain": "train", "refresh": True}]}
            ],
        },
        {**turn, "operation_id": "amend"},
    )
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "刷新车次"}, seed["trip_plan_id"]
    )
    assert refreshed["stage"] == payload["tripplan"]["stage"] == "awaiting_query"
    assert not refreshed["candidates"] and (not payload["tripplan"]["candidates"])
    assert payload["tripplan"]["candidate_queries"] == {} and len(train.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("city", ["上海", "北京"])
async def test_review_can_retry_only_previously_failed_independent_query(tmp_path, city):
    from datetime import UTC, datetime

    from trip_assistant.providers.contracts import AgentDataResult, Source

    class Public:
        calls = 0

        async def weather(self, city, day):
            self.calls += 1
            source = Source("simulation:weather", datetime.now(UTC), "https://weather.example.test")
            return AgentDataResult(
                "error" if self.calls == 1 else "ok",
                {"city": city},
                []
                if self.calls == 1
                else [{"temp_c": 20, "description": "晴", "source": source.to_dict()}],
                [],
                source,
                source.fetched_at,
                "模拟首次失败" if self.calls == 1 else None,
            )

    public = Public()
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120), "public": public}
    )

    def retry(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["query_results"][0]["status"] == "partial"
        return batch(
            (
                "retry",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [
                        {"domain": "weather", "parameters": {"city": city}, "refresh": True}
                    ],
                },
            )
        )

    runtime.model.script = [
        batch(
            ("p", "write_trip", single_trip()),
            (
                "weather",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [{"domain": "weather", "parameters": {"city": "上海"}}],
                },
            ),
        ),
        retry,
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    result = await runtime.run("查上海天气并规划重庆去上海单程")
    assert result["status"] == ("completed" if city == "上海" else "partial")
    assert public.calls == (2 if city == "上海" else 1)
