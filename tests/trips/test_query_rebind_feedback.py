from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture, trip_observation, write_then_query
from test_activities import context
from test_activity_save_consistency import choose
from test_policy_repair_convergence import proposal

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.errors import ToolFailure


async def prepared(runtime):
    plan = proposal("visit")
    plan["tasks"][1]["conditions"]["constraints"] = {"departure_time_after": "13:00"}
    plan["tasks"][1]["condition_strengths"]["departure_time_after"] = "soft"
    plan["tasks"][1]["items"][0]["constraints"] = {"departure_time_after": "13:00"}
    turn = context(runtime)
    trip = await write_then_query(runtime.trip_service, plan, turn)
    turn["operation_id"] = "t:choose"
    selected = choose(trip)
    view = next(v for v in trip["candidates"] if v["task_id"] == trip["tasks"][1]["id"])
    candidate = next(i for i in view["items"] if i["departure_at"][11:16] == "13:00")
    selected["tasks"][1]["selection_refs"]["train"]["candidate_id"] = candidate["id"]
    chosen = await write_then_query(runtime.trip_service, selected, turn)
    assert chosen["status"] == "completed"
    return trip, chosen, turn, selected


@pytest.mark.asyncio
async def test_changed_query_binding_is_not_reported_as_expired_fresh_evidence(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=120, departure_hours=[7, 13]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    _, chosen, turn, old_selection = await prepared(runtime)
    saved = deepcopy(runtime.trip_service.store.load(chosen["trip_plan_id"]))
    turn["operation_id"] = "t:change-and-old-ref"
    bad = {
        "operation": "amend",
        "trip_plan_id": chosen["trip_plan_id"],
        "expected_revision": chosen["revision"],
        "tasks": [
            {
                "task_id": chosen["tasks"][1]["id"],
                "components": ["train"],
                "conditions": {"constraints": {"departure_time_after": "12:00"}},
                "selection_refs": old_selection["tasks"][1]["selection_refs"],
            }
        ],
    }
    with pytest.raises(ToolFailure) as caught:
        await runtime.trip_service.apply(bad, turn)
    assert caught.value.code == "QUERY_REBIND_REQUIRED"
    assert (
        runtime.trip_service.store.load(chosen["trip_plan_id"])["committed"] == saved["committed"]
    )


@pytest.mark.asyncio
async def test_model_can_separate_amend_query_and_selection_without_false_automatic_expiry(
    tmp_path,
):
    train, hotel = (
        SimulatedTrainProvider(duration_minutes=120, departure_hours=[7, 13]),
        SimulatedHotelProvider(),
    )
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    _, chosen, _, old_selection = await prepared(runtime)

    def tool(identity, args, final=None):
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": identity,
                    "name": "write_trip",
                    "input": {
                        "request_kind": "planning",
                        "tripplan": args,
                        **(
                            {
                                "final_answer": final,
                                "goal_assessment": {
                                    "status": "satisfied",
                                    "reason": "原需求与重新绑定后的选择相符。",
                                },
                            }
                            if final
                            else {}
                        ),
                    },
                }
            ]
        )

    def amend(messages, include_old_selection):
        trip = trip_observation(messages)
        row = {
            "task_id": trip["tasks"][1]["id"],
            "components": ["train"],
            "conditions": {"constraints": {"departure_time_after": "12:00"}},
            "items": [{"domain": "train", "constraints": {"departure_time_after": "12:00"}}],
        }
        if include_old_selection:
            row["selection_refs"] = old_selection["tasks"][1]["selection_refs"]
        return tool(
            "bad" if include_old_selection else "fixed",
            {
                "operation": "amend",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": [row],
            },
        )

    runtime.model.script = [
        lambda m: amend(m, True),
        lambda m: amend(m, False),
        lambda m: tool("selected", choose(trip_observation(m)), "已按新的出发时段更新并保存。"),
    ]
    result = await runtime.run(
        "把返程出发时段软建议调整为12点后",
        context={
            "review_trip_plan_id": chosen["trip_plan_id"],
        },
    )
    assert result["status"] == "completed"
    assert result["tool_results"][0]["error"]["code"] == "QUERY_REBIND_REQUIRED"
    assert result["stop_reason"] is None and len(train.calls) == 2 and len(hotel.calls) == 1
    assert not any(
        j.get("job_type") == "automatic_evidence_refresh" for j in result["execution_jobs"]
    )
