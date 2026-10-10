import json

import pytest


def response(value, stage="main:plan"):
    return {"stage": stage, "text": json.dumps(value)}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"intents": []},
        {"intents": [{"type": "replace_hotel"}]},
        {"intents": [{"type": "control"}]},
        {"intents": ["update"]},
    ],
)
def test_live_intent_assessment_rejects_missing_old_or_wrong_types(value):
    from evals.run_preflight_simulated import assess_intents

    assert not assess_intents("hotel_replace", [response(value)])["passed"]


def test_combined_request_requires_both_purposes_and_preference_action():
    from evals.run_preflight_simulated import assess_intents

    value = {
        "intents": [{"type": "update"}, {"type": "plan"}],
        "agent_schedule": [{"agent_name": "preference"}],
        "workflow_proposal": {"tasks": []},
    }
    assert assess_intents("preference_plan", [response(value)])["passed"]
    value["agent_schedule"] = []
    assert not assess_intents("preference_plan", [response(value)])["passed"]
    value["agent_schedule"] = [{"agent_name": "preference"}]
    value["intents"] = [{"type": "plan"}]
    assert not assess_intents("preference_plan", [response(value)])["passed"]


@pytest.mark.parametrize("case,kind", [("explain", "ask"), ("unsupported_purchase", "control")])
def test_explain_and_unsupported_action_require_direct_without_dispatch(case, kind):
    from evals.run_preflight_simulated import assess_intents

    value = dict(intents=[{"type": kind}], response_mode="direct", agent_schedule=[])
    assert assess_intents(case, [response(value)])["passed"]
    value.update(response_mode="workflow", resume_workflow_id="w1")
    assert not assess_intents(case, [response(value)])["passed"]


def test_repaired_initial_decision_is_checked_but_later_steps_cannot_hide_failure():
    from evals.run_preflight_simulated import assess_intents

    records = [
        response({"intents": [{"type": "replace_hotel"}]}),
        response({"intents": [{"type": "update"}]}, "main:repair_decision"),
    ]
    assert assess_intents("hotel_replace", records)["passed"]
    assert not assess_intents(
        "hotel_replace", [records[0], response({"intents": [{"type": "update"}]}, "main:step")]
    )["passed"]


def test_condition_change_case_changes_the_actual_previous_stay():
    from evals.run_preflight_simulated import case_query

    previous = {"workflow": {"tasks": [{"conditions": {"nights": 3}}]}}
    assert "4晚" in case_query("change_stay", "改为3晚", previous)
    assert case_query("explain", "解释原因", previous) == "解释原因"


def test_memory_read_accepts_known_context_but_never_preference_write():
    from types import SimpleNamespace

    from evals.run_preflight_simulated import assess, assess_intents

    value = dict(
        intents=[{"type": "ask"}],
        response_mode="direct",
        agent_schedule=[],
        final_answer="已保存偏好为全季",
    )
    assert assess_intents("memory_read", [response(value)])["passed"]
    run = SimpleNamespace(
        external_request_count=0, effective_preferences={"hotel_brands": ["全季"]}
    )
    assert assess("memory_read", {"status": "ok", "final_answer": value["final_answer"]}, run)[
        "passed"
    ]
    assert not assess("memory_read", {"status": "ok", "final_answer": "已保存偏好为如家"}, run)[
        "passed"
    ]
    value["agent_schedule"] = [{"agent_name": "preference"}]
    assert not assess_intents("memory_read", [response(value)])["passed"]
