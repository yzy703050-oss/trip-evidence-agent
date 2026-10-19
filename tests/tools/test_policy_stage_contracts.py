from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from trip_assistant.tools.registry import build_registry


def create_args(purpose):
    return {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "purpose": purpose,
                    "conditions": {"departure_date": "2026-10-19"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
    }


@pytest.mark.parametrize("observed", [False, True])
def test_pre_policy_create_rejects_business_but_allows_tourism(observed):
    from trip_assistant.tools.registry import creation_registry

    original = build_registry({})
    schema_before = deepcopy(original["write_trip"].schema)
    projected = creation_registry(original, policy_observed=observed)
    tool = projected["write_trip"]
    validator = Draft202012Validator(tool.schema["function"]["parameters"])
    assert validator.is_valid(create_args("visit"))
    tool.validate(create_args("visit"))
    assert validator.is_valid(create_args("business")) is observed
    if observed:
        tool.validate(create_args("business"))
    else:
        with pytest.raises(ValueError):
            tool.validate(create_args("business"))
    assert original["write_trip"].schema == schema_before
    assert projected["query_policy"] is original["query_policy"]


@pytest.mark.parametrize("source", ["derived", "proposal", "default", "preference"])
def test_model_inferred_exact_deadline_cannot_be_a_hard_requirement(source):
    args = create_args("business")
    task = args["tripplan"]["tasks"][0]
    task.update(
        conditions={**task["conditions"], "arrival_before": "2026-10-19T09:00:00+08:00"},
        field_sources={"arrival_before": source},
        condition_strengths={"arrival_before": "hard"},
    )
    with pytest.raises(ValueError, match="inferred arrival deadline"):
        build_registry({})["write_trip"].validate(args)


@pytest.mark.parametrize("source,strength", [("user", "hard"), ("derived", "soft")])
def test_explicit_user_deadline_and_soft_suggestion_still_work(source, strength):
    args = create_args("business")
    task = args["tripplan"]["tasks"][0]
    task.update(
        conditions={**task["conditions"], "arrival_before": "2026-10-19T09:00:00+08:00"},
        field_sources={"arrival_before": source},
        condition_strengths={"arrival_before": strength},
    )
    assert build_registry({})["write_trip"].validate(args)


def test_inferred_deadline_cannot_bypass_check_via_global_conditions():
    args = create_args("business")
    args["tripplan"].update(
        confirmed_conditions={"arrival_before": "2026-10-19T09:00:00+08:00"},
        field_sources={"arrival_before": "derived"},
    )
    with pytest.raises(ValueError, match="inferred arrival deadline"):
        build_registry({})["write_trip"].validate(args)
