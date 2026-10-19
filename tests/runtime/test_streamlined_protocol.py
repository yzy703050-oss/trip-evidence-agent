import json

import pytest
from support.native import NativeRuntimeFixture
from test_goal_assessment import prepared_selection

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.tools.registry import build_registry, trip_review_registry
from trip_assistant.tools.trip_write import build_trip_writer


def test_main_and_review_do_not_offer_candidate_pagination_or_self_rating():
    registry = build_registry({})
    schema = registry["read"].schema["function"]["parameters"]
    assert "candidates" not in schema["properties"]["resource"]["enum"]
    assert "query_id" not in schema["properties"]
    assert "read" not in trip_review_registry(registry, "trip", {"q": 1})
    assert "goal_assessment" not in json.dumps(registry["write_trip"].schema)


def test_critical_argument_guidance_survives_schema_compaction_and_stage_binding():
    registry = build_registry({})
    review = trip_review_registry(registry, "trip")
    for tool in (registry["write_trip"], review["write_trip"]):
        description = tool.schema["function"]["description"]
        assert "condition_strengths" in description and "field_sources" in description
        assert "confirmed_conditions.start_date" in description
        assert "default_round_trip" in description


async def test_selection_without_rating_saves_and_existing_version_can_be_adjusted(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    writer = build_trip_writer(runtime.trip_service)
    first = await writer({"tripplan": plan}, {"operation_id": "select"})
    assert first["status"] == "completed"
    change = {
        "operation": "amend",
        "trip_plan_id": first["trip_plan_id"],
        "expected_revision": first["revision"],
        "tasks": [
            {
                "task_id": plan["tasks"][0]["task_id"],
                "components": ["content"],
                "content": [
                    {
                        "purpose": "散步",
                        "description": "抵达后散步",
                        "source": "proposal",
                        "strength": "soft",
                    }
                ],
            }
        ],
    }
    second = await writer({"tripplan": change}, {"operation_id": "amend"})
    assert second["status"] == "completed" and second["revision"] > first["revision"]


@pytest.mark.parametrize("rating", ["partial", "unmet", "unknown", "satisfied"])
async def test_legacy_rating_is_compatible_but_not_a_write_gate(tmp_path, rating):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    result = await build_trip_writer(runtime.trip_service)(
        {
            "tripplan": await prepared_selection(runtime),
            "goal_assessment": {"status": rating, "reason": "旧标签"},
        },
        {"operation_id": "select"},
    )
    assert result["status"] == "completed"
    assert "goal_assessment" not in result["validation"]
