from copy import deepcopy
import json

import pytest


@pytest.mark.parametrize("status", ["completed", "partial", "needs_input"])
def test_result_projection_keeps_reply_and_facts_without_mutation(status):
    from agents.context_projection import project_session_result, project_session_message

    result = {
        "status": status,
        "final_answer": "已安排",
        "finalization_method": "workflow",
        "workflow_id": "w",
        "workflow_revision": 12,
        "travel_conditions": {"origin": "北京", "destination": "上海"},
        "itinerary": {"daily_plans": [{"day": 1}]},
        "workflow": {
            "results_by_query": {
                "hotel": {"items": [{"id": str(n), "raw": "x" * 1000} for n in range(500)]}
            }
        },
        "results": [{"raw": "x" * 2000}],
        "domain_results": {"raw": "x" * 2000},
        "validated_plan": {"raw": "x" * 2000},
    }
    original = deepcopy(result)
    small = project_session_result(result)
    assert result == original
    assert {
        "workflow",
        "results",
        "domain_results",
        "validated_plan",
        "candidate_cache",
    }.isdisjoint(small)
    assert len(json.dumps(small)) < len(json.dumps(result)) / 10
    old = {
        "scope": "session",
        "type": "message",
        "role": "assistant",
        "content": json.dumps(result),
        "metadata": {"format": "main_result"},
    }
    new = {**old, "content": json.dumps(small)}
    assert project_session_message(old) == project_session_message(new)
    assert small["workflow_revision"] == 12


def test_error_projection_keeps_diagnostic_code():
    from agents.context_projection import project_session_result

    small = project_session_result(
        {
            "status": "error",
            "final_answer": "请补充信息",
            "error_code": "invalid_plan",
            "stop_reason": "invalid_plan",
            "results": [{"raw": "private"}],
        }
    )
    assert small["error_code"] == "invalid_plan"
    assert small["stop_reason"] == "invalid_plan"
