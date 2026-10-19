import pytest
from support.native import NativeRuntimeFixture, write_then_query
from test_activities import context
from test_activity_save_consistency import choose
from test_policy_repair_convergence import proposal

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider


@pytest.mark.asyncio
async def test_next_day_morning_return_keeps_hard_morning_meeting_without_repair(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[7])
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    turn = context(runtime, policy_context={"status": "ok", "constraints": [], "references": []})
    plan = proposal()
    plan["tasks"][0]["content"][0]["time"] = "上午"
    plan["tasks"][0]["conditions"].update(check_out="2026-10-19", nights=2)
    plan["tasks"][1]["conditions"]["departure_date"] = "2026-10-19"
    proposed = await write_then_query(runtime.trip_service, plan, turn)
    turn["operation_id"] = "t:select-next-day"
    selected = await write_then_query(runtime.trip_service, choose(proposed), turn)
    assert selected["status"] == "completed", selected["validation"]
    assert selected["tasks"][0]["content"] == proposed["tasks"][0]["content"]
    returning = selected["validation"]["reconstructed_tasks"][1]["train"]
    assert returning["departure_at"] == "2026-10-19T07:00:00+08:00"
    assert len(train.calls) == 2 and len(hotel.calls) == 1


@pytest.mark.asyncio
async def test_return_departure_time_constraint_only_invalidates_train(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[7, 13])
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    turn = context(runtime)
    written = await write_then_query(runtime.trip_service, proposal("visit"), turn)
    turn["operation_id"] = "t:select-bad-return"
    selected = await write_then_query(runtime.trip_service, choose(written), turn)
    before = runtime.trip_service.store.load(selected["trip_plan_id"])
    turn["operation_id"] = "t:repair-return-time"
    repaired = await write_then_query(
        runtime.trip_service,
        {
            "operation": "amend",
            "trip_plan_id": selected["trip_plan_id"],
            "expected_revision": selected["revision"],
            "tasks": [
                {
                    "task_id": selected["tasks"][1]["id"],
                    "components": ["train"],
                    "conditions": {"constraints": {"departure_time_after": "12:00"}},
                    "items": [
                        {"domain": "train", "constraints": {"departure_time_after": "12:00"}}
                    ],
                }
            ],
        },
        turn,
    )
    after = runtime.trip_service.store.load(repaired["trip_plan_id"])
    assert after["tasks"][0] == before["tasks"][0]
    assert len(hotel.calls) == 1
    turn["operation_id"] = "t:choose-later-return"
    view = next(
        v
        for v in repaired["candidates"]
        if v["task_id"] == repaired["tasks"][1]["id"]
        and v["items"]
        and v["items"][0]["departure_at"][11:16] == "13:00"
    )
    selection = choose(repaired)
    selection["tasks"][1]["selection_refs"]["train"] = {
        "query_id": view["query_id"],
        "result_revision": view["result_revision"],
        "candidate_id": view["items"][0]["id"],
    }
    chosen = await write_then_query(runtime.trip_service, selection, turn)
    assert chosen["status"] == "completed"
    assert chosen["tasks"][0]["content"] == selected["tasks"][0]["content"]


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("departure_time_after", "12:00", {"train"}),
        ("hotel_max_total_cny", 600, {"hotel"}),
        ("available_only", True, {"train", "hotel"}),
    ],
)
def test_constraint_domains_do_not_invalidate_unrelated_component(field, value, expected):
    from trip_assistant.trips.dependencies import affected_components

    old = {"seat_class": "二等座", "hotel_max_total_cny": 500}
    task = {"conditions": {"constraints": old}}
    assert (
        affected_components(task, {"conditions": {"constraints": {**old, field: value}}})
        == expected
    )


def test_whole_constraints_metadata_keeps_train_only_scope_when_all_constraints_are_train():
    from trip_assistant.trips.dependencies import affected_components

    task = {"conditions": {"constraints": {"seat_class": "二等座"}}}
    proposed = {
        "conditions": {"constraints": {"seat_class": "二等座", "departure_time_after": "12:00"}},
        "condition_strengths": {"constraints": "soft"},
    }
    assert affected_components(task, proposed) == {"train"}


def test_activity_feedback_identifies_destination_and_repairable_proposal():
    from trip_assistant.trips.activities import activity_issues

    task = {
        "id": "return",
        "origin": "上海",
        "destination": "重庆",
        "content": [
            {
                "id": "checkout",
                "date": "2026-10-21",
                "time": "07:20",
                "purpose": "退房返程",
                "description": "清晨退房后乘车返程",
                "source": "proposal",
                "strength": "soft",
            }
        ],
    }
    issues = activity_issues(task, {"schedule": {"arrival_at": "2026-10-21T18:50:00+08:00"}})
    assert issues[0]["activity_scope"] == {"destination": "重庆", "kind": "destination_activity"}
    assert issues[0]["activity"]["id"] == "checkout"
    assert issues[0]["repair_permissions"]["automatic_activity_edit"] is True
    task["content"][0]["strength"] = "hard"
    assert not activity_issues(
        task,
        {
            "schedule": {
                "arrival_at": "2026-10-21T18:50:00+08:00",
            }
        },
    )[0]["repair_permissions"]["automatic_activity_edit"]


@pytest.mark.asyncio
async def test_one_amend_can_extend_stay_requery_return_and_remove_auxiliary_activity(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[7])
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    turn = context(runtime)
    original = proposal("visit")
    original["tasks"][1]["content"] = [
        {
            "date": "2026-10-18",
            "time": "07:00",
            "purpose": "退房取行李",
            "description": "在上海退房后前往车站",
            "source": "proposal",
            "strength": "soft",
        }
    ]
    written = await write_then_query(runtime.trip_service, original, turn)
    turn["operation_id"] = "t:choose-conflicting"
    blocked = await write_then_query(runtime.trip_service, choose(written), turn)
    assert any(i["code"] == "activity_before_arrival" for i in blocked["validation"]["issues"])
    turn["operation_id"] = "t:complete-local-repair"
    repaired = await write_then_query(
        runtime.trip_service,
        {
            "operation": "amend",
            "trip_plan_id": blocked["trip_plan_id"],
            "expected_revision": blocked["revision"],
            "tasks": [
                {
                    "task_id": blocked["tasks"][0]["id"],
                    "components": ["hotel"],
                    "conditions": {"check_out": "2026-10-19", "nights": 2},
                    "items": [{"domain": "hotel"}],
                },
                {
                    "task_id": blocked["tasks"][1]["id"],
                    "components": ["train", "content"],
                    "conditions": {"departure_date": "2026-10-19"},
                    "items": [{"domain": "train"}],
                    "content": [],
                },
            ],
        },
        turn,
    )
    # Hotel-place evidence has no dated quote, so its valid cached result is reusable.
    assert len(hotel.calls) == 1 and len(train.calls) == 3
    assert repaired["tasks"][0]["content"] == blocked["tasks"][0]["content"]
    assert repaired["tasks"][0]["conditions"]["check_out"] == "2026-10-19"
    assert repaired["tasks"][1]["content"] == []
    turn["operation_id"] = "t:select-repaired"
    selected = await write_then_query(runtime.trip_service, choose(repaired), turn)
    assert selected["status"] == "completed", selected["validation"]
    assert len(hotel.calls) == 1 and len(train.calls) == 3
