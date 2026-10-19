from copy import deepcopy

import pytest


def test_patch_normalization_preserves_explicit_semantics():
    from trip_assistant.trips.patches import normalize_trip_patch

    saved = {
        "id": "trip",
        "revision": 3,
        "confirmed_conditions": {},
        "tasks": [
            {
                "id": "task",
                "origin": "重庆",
                "purpose": "visit",
                "conditions": {"passengers": 1},
                "field_sources": {"origin": "preference"},
                "content": [{"id": "activity"}],
            }
        ],
    }
    patch = {
        "operation": "amend",
        "trip_plan_id": "trip",
        "expected_revision": 3,
        "tasks": [
            {
                "task_id": "task",
                "origin": "重庆",
                "purpose": "visit",
                "conditions": {"passengers": 1},
                "field_sources": {"origin": "user"},
                "content": [],
                "items": [{"domain": "train", "refresh": True}],
            }
        ],
    }
    original = deepcopy(patch)
    result = normalize_trip_patch(saved, patch)
    row = result["tasks"][0]
    assert "origin" not in row and "purpose" not in row and "conditions" not in row
    assert row["content"] == [] and row["field_sources"] == {"origin": "user"}
    assert row["items"][0]["refresh"] and patch == original
    with pytest.raises(ValueError, match="revision"):
        normalize_trip_patch({**saved, "revision": 4}, patch)


def test_allowed_null_and_empty_patch_values_are_preserved():
    from trip_assistant.tools.registry import build_registry
    from trip_assistant.trips.patches import normalize_trip_patch

    args = {
        "request_kind": "planning",
        "goal_assessment": {
            "status": "satisfied",
            "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
        },
        "tripplan": {
            "operation": "amend",
            "trip_plan_id": "t",
            "expected_revision": 1,
            "route_tasks": None,
            "tasks": [
                {"task_id": "a", "origin": None, "content": [], "conditions": {"constraints": {}}}
            ],
        },
    }
    plan = build_registry({})["write_trip"].validate(args)["tripplan"]
    assert "route_tasks" in plan and plan["route_tasks"] is None
    normalized = normalize_trip_patch(
        {"revision": 1, "tasks": [{"id": "a", "origin": "重庆"}]}, plan
    )
    assert normalized["tasks"][0]["origin"] is None
    assert normalized["tasks"][0]["content"] == []
    assert normalized["tasks"][0]["conditions"] == {"constraints": {}}


@pytest.mark.parametrize(
    "source,level,removed",
    [("proposal", "soft", True), ("user", "soft", False), ("proposal", "hard", False)],
)
def test_repeating_effective_soft_strength_does_not_invalidate_selection(source, level, removed):
    from trip_assistant.trips.patches import normalize_trip_patch

    saved = {
        "revision": 1,
        "confirmed_conditions": {},
        "tasks": [
            {
                "id": "t",
                "conditions": {"departure_date": "2026-10-21"},
                "field_sources": {"departure_date": source},
                "condition_strengths": {},
            }
        ],
    }
    patch = {
        "operation": "amend",
        "trip_plan_id": "p",
        "expected_revision": 1,
        "tasks": [
            {"task_id": "t", "condition_strengths": {"departure_date": level}, "content": []}
        ],
    }
    row = normalize_trip_patch(saved, patch)["tasks"][0]
    assert ("condition_strengths" not in row) is removed
