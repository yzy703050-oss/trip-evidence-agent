from copy import deepcopy

import pytest

from agents.contracts import validate_plan


def direct(**changes):
    return dict(response_mode="direct", final_answer="你好", agent_schedule=[], **changes)


@pytest.mark.parametrize(
    "intents",
    [
        [{"type": "replace_hotel"}],
        [{"type": "unsupported_action"}],
        "ask",
        ["ask"],
        [None],
        [{}],
        [{"type": ["ask"]}],
        None,
    ],
)
def test_rejects_unknown_or_malformed_intents(intents):
    with pytest.raises(ValueError, match="intent"):
        validate_plan(direct(intents=intents))


@pytest.mark.parametrize("kind", ["ask", "plan", "update", "control"])
def test_accepts_only_four_purpose_types_and_removes_descriptions(kind):
    result = validate_plan(
        direct(intents=[{"type": kind, "description": "旧描述"}, {"type": kind}])
    )
    assert result["intents"] == [{"type": kind}]


def test_missing_intents_are_inferred_without_changing_execution():
    value = dict(
        response_mode="answer",
        agent_schedule=[{"agent_name": "information_query", "requested_domains": ["weather"]}],
    )
    original = deepcopy(value)
    result = validate_plan(value)
    assert result["intents"] == [{"type": "ask"}]
    assert result["agent_schedule"][0]["requested_domains"] == ["weather"]
    assert value == original
    assert validate_plan({**value, "intents": []}) == result


def test_combined_preference_and_planning_keeps_both_purposes():
    value = dict(
        response_mode="workflow",
        workflow_proposal={"tasks": []},
        agent_schedule=[{"agent_name": "preference"}],
    )
    result = validate_plan(value)
    assert result["intents"] == [{"type": "update"}, {"type": "plan"}]
    assert result["workflow_proposal"] == value["workflow_proposal"]
    assert result["agent_schedule"][0]["agent_name"] == "preference"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("replace", "update"),
        ("regenerate", "update"),
        ("change_route", "update"),
        ("supplement", "update"),
        ("adopt", "control"),
        ("pause", "control"),
        ("cancel", "control"),
    ],
)
def test_update_operation_is_separate_from_purpose(kind, expected):
    update = dict(update_type=kind, target={"workflow_id": "w1"})
    result = validate_plan(dict(travel_update=update))
    assert result["intents"] == [{"type": expected}]
    assert result["travel_update"] == update


def test_preference_and_trip_update_share_type_but_keep_both_actions():
    update = dict(update_type="replace", target={"workflow_id": "w1", "components": ["hotel"]})
    result = validate_plan(
        dict(travel_update=update, agent_schedule=[{"agent_name": "preference"}])
    )
    assert result["intents"] == [{"type": "update"}]
    assert result["travel_update"] == update
    assert result["agent_schedule"][0]["agent_name"] == "preference"


@pytest.mark.parametrize(
    "value,expected",
    [
        (direct(), ["ask"]),
        (direct(feedback_scope={"workflow_id": "w1", "question": "改哪段？"}), ["update"]),
        (dict(response_mode="answer", agent_schedule=[{"agent_name": "preference"}]), ["update"]),
        (
            dict(
                response_mode="answer",
                agent_schedule=[{"agent_name": "preference"}, {"agent_name": "memory_query"}],
            ),
            ["update", "ask"],
        ),
        (dict(response_mode="workflow", resume_workflow_id="w1"), ["control"]),
        (
            dict(
                response_mode="workflow",
                resume_workflow_id="w1",
                workflow_update={"task_updates": [{"task_id": "t1", "conditions": {"nights": 2}}]},
            ),
            ["update"],
        ),
        (dict(response_mode="itinerary"), ["plan"]),
    ],
)
def test_legacy_routing_infers_purpose(value, expected):
    assert validate_plan(value)["intents"] == [{"type": kind} for kind in expected]


@pytest.mark.parametrize(
    "value",
    [
        dict(
            travel_update={
                "update_type": "change",
                "target": {"workflow_id": "w1"},
                "condition_updates": {},
            }
        ),
        dict(response_mode="workflow"),
        dict(agent_schedule=[{"agent_name": "purchase"}]),
    ],
)
def test_intents_cannot_bypass_execution_validation(value):
    with pytest.raises(ValueError):
        validate_plan({**value, "intents": [{"type": "control"}]})
