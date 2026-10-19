from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture, write_then_query
from test_activities import context
from test_activity_save_consistency import choose

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider


def proposal(purpose="business", *, meeting_day="2026-10-18"):
    return {
        "operation": "propose",
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": purpose,
                "requires_hotel": True,
                "conditions": {
                    "departure_date": "2026-10-17",
                    "check_in": "2026-10-17",
                    "check_out": "2026-10-18",
                    "nights": 1,
                },
                "condition_strengths": {
                    "departure_date": "soft",
                    "check_in": "soft",
                    "check_out": "soft",
                    "nights": "soft",
                },
                "content": [
                    {
                        "date": meeting_day,
                        "time": "08:00",
                        "purpose": "会议",
                        "description": "上海会议",
                        "strength": "hard",
                        "source": "user",
                    }
                ],
                "items": [{"domain": "train"}, {"domain": "hotel"}],
            },
            {
                "origin": "上海",
                "destination": "重庆",
                "purpose": "return",
                "conditions": {"departure_date": "2026-10-18"},
                "condition_strengths": {"departure_date": "soft"},
                "items": [{"domain": "train"}],
            },
        ],
    }


@pytest.mark.asyncio
async def test_policy_gap_does_not_stop_soft_return_repair_and_policy_only_then_stops(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=120, departure_hours=[7]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    turn = context(
        runtime,
        policy_context={"status": "no_knowledge", "missing_fields": ["policy"], "constraints": []},
    )
    proposed = await write_then_query(runtime.trip_service, proposal(), turn)
    turn["operation_id"] = "t:bad-select"
    blocked = await write_then_query(runtime.trip_service, choose(proposed), turn)
    assert {i["code"] for i in blocked["validation"]["issues"]} >= {
        "policy_unverified",
        "activity_after_departure",
    }
    assert not blocked["stop_planning"]
    assert not runtime.trip_service.store.load(blocked["trip_plan_id"]).get("committed")
    turn["operation_id"] = "t:repair"
    repaired = await write_then_query(
        runtime.trip_service,
        {
            "operation": "amend",
            "trip_plan_id": blocked["trip_plan_id"],
            "expected_revision": blocked["revision"],
            "tasks": [
                {
                    "task_id": blocked["tasks"][1]["id"],
                    "components": ["train"],
                    "conditions": {"departure_date": "2026-10-19"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
        turn,
    )
    turn["operation_id"] = "t:select-repaired"
    chosen = await write_then_query(runtime.trip_service, choose(repaired), turn)
    blocking = [i["code"] for i in chosen["validation"]["issues"] if i["blocking"]]
    assert blocking == ["policy_unverified"]
    assert chosen["status"] == "partial" and chosen["stop_planning"]
    assert chosen["stop_reason"] == "policy_unverified"
    assert chosen["tasks"][0]["content"] == blocked["tasks"][0]["content"]


@pytest.mark.asyncio
async def test_hard_meeting_with_soft_transport_date_can_be_repaired(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=720, departure_hours=[7]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    turn = context(runtime)
    written = await write_then_query(
        runtime.trip_service, proposal("visit", meeting_day="2026-10-17"), turn
    )
    turn["operation_id"] = "t:select"
    selected = await write_then_query(runtime.trip_service, choose(written), turn)
    assert "activity_before_arrival" in {i["code"] for i in selected["validation"]["issues"]}
    assert not selected["stop_planning"]


@pytest.mark.asyncio
async def test_explicit_disable_repair_still_stops_mixed_policy_gap(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=120, departure_hours=[7]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    turn = context(runtime, policy_context={"status": "no_knowledge", "constraints": []})
    written = await write_then_query(runtime.trip_service, proposal(), turn)
    turn["operation_id"] = "t:stop"
    args = deepcopy(choose(written))
    args["allow_repair"] = False
    selected = await write_then_query(runtime.trip_service, args, turn)
    assert selected["stop_planning"] and selected["stop_reason"] == "repair_disabled"


@pytest.mark.asyncio
async def test_fixed_departure_with_all_trains_after_hard_meeting_stops_without_replanning(
    tmp_path,
):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=720, departure_hours=[7, 8]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    plan = proposal("visit", meeting_day="2026-10-17")
    plan["tasks"][0]["condition_strengths"]["departure_date"] = "hard"
    written = await write_then_query(runtime.trip_service, plan, context(runtime))
    assert written["stop_planning"] and written["stop_reason"] == "hard_constraint_conflict"
    assert "hard_date_conflict" in {i["code"] for i in written["validation"]["issues"]}
    assert written["tasks"][0]["content"][0]["time"] == "08:00"
