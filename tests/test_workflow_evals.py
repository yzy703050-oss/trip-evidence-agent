"""Offline regression scenarios grouped by responsibility."""

from copy import deepcopy

from support.weather import weather_info
from support.workflow import populated_workflow

from evals.native_checks import evaluate_result
from evals.v0_memory.runner import evaluate_case, sourced_output_valid
from trip_assistant.storage.sessions import SessionStore
from trip_assistant.trips.validation import check_workflow, finalize_workflow

# workflow_evals


def envelope():
    w = populated_workflow()
    checked = check_workflow(w)
    w = finalize_workflow(w, checked)
    return dict(
        status="completed",
        workflow=w,
        validated_plan=checked,
        stop_reason="validated",
        final_answer="安排已整理",
    )


def test_evaluator_requires_current_global_validation():
    valid = envelope()
    assert evaluate_result(valid, expected_tasks=3)["passed"] is True
    for mutate in [
        lambda r: r["workflow"]["tasks"][0].update(status="draft"),
        lambda r: r["workflow"]["tasks"][0].update(revision=2),
        lambda r: r["workflow"].update(validation=None),
        lambda r: r["workflow"]["tasks"][0]["draft_plan"].update(activities=[]),
        lambda r: next(
            q for q in r["workflow"]["results_by_query"].values() if q["domain"] == "hotel"
        )["items"][0].update(stay_total_cny="999"),
    ]:
        bad = deepcopy(valid)
        mutate(bad)
        assert evaluate_result(bad, expected_tasks=3)["passed"] is False


def test_needs_input_is_a_pause_not_a_query_success():
    value = envelope()
    value["status"] = "needs_input"
    value["workflow"]["status"] = "needs_input"
    value["workflow"]["checkpoint"] = {"question": "哪天出发？"}
    checked = evaluate_result(value, expected_tasks=3)
    assert checked["passed"] is True
    assert checked["planning_complete"] is False
    assert checked["external_query_verified"] is False


def test_query_success_and_planning_completion_are_measured_separately():
    value = envelope()
    value["status"] = "needs_input"
    value["workflow"]["checkpoint"] = {"question": "请确认日期"}
    row = {"status": "ok", "items": [{"id": "sourced"}], "source": {"provider": "fixture"}}
    value["results"] = [{"data": {"query_results": [row]}}]
    result = evaluate_result(value, expected_tasks=3)
    assert result["external_query_verified"] is True
    assert result["planning_complete"] is False
    assert (
        evaluate_result({"status": "ok", "domain_results": {"train": row}})[
            "external_query_verified"
        ]
        is True
    )


# main_evals


def test_evaluation_detects_failed_internal_tool(tmp_path):
    store = SessionStore(tmp_path, "alice", "tools")
    store.append_run(
        {"type": "agent_plan", "turn_id": "a", "agents": [{"agent_name": "information_query"}]}
    )
    store.append(
        {
            "type": "stage_complete",
            "turn_id": "a",
            "agent_name": "information_query",
            "status": "success",
            "content": {"data": weather_info()},
        }
    )
    store.append(
        {
            "type": "tool_call",
            "turn_id": "a",
            "scope": "agent:information_query:1",
            "tool_calls": [{"id": "failed", "name": "weather"}],
        }
    )
    store.append(
        {
            "type": "tool_result",
            "tool_call_id": "failed",
            "turn_id": "a",
            "scope": "agent:information_query:1",
            "status": "error",
            "content": {"status": "error"},
        }
    )
    store.append(
        {
            "type": "stage_complete",
            "turn_id": "a",
            "agent_name": "main_finalize",
            "status": "success",
            "content": {"data": {"final_answer": "答复"}},
        }
    )
    store.append_run({"type": "query_run", "turn_id": "a"})
    result = evaluate_case({"id": "tools", "expected_agents": ["information_query"]}, store)
    assert result["checks"]["executed_route"] is True
    assert result["checks"]["execution_success"] is False


def test_nested_forged_facts_are_rejected():
    info = weather_info()
    info["domain_results"]["weather"]["items"][0]["date"] = "wrong"
    assert not sourced_output_valid(
        [{"agent_name": "information_query", "content": {"data": info}}]
    )


# sourced_evals


def test_edd_rejects_unsourced_itinerary_prices():
    stages = [
        {
            "agent_name": "itinerary_planning",
            "content": {"data": {"itinerary": {"estimated_budget": "今日车票299元"}}},
        }
    ]
    assert sourced_output_valid(stages) is False


def test_edd_accepts_guarded_suggestion():
    from trip_assistant.providers.plan_guard import guard_itinerary

    stages = [{"agent_name": "itinerary_planning", "content": {"data": guard_itinerary({}, [])}}]
    assert sourced_output_valid(stages) is True


def test_edd_rejects_standalone_unsourced_offer():
    stages = [
        {
            "agent_name": "train_search",
            "content": {"data": {"status": "ok", "items": [{"id": "bad", "price_cny": "299"}]}},
        }
    ]
    assert sourced_output_valid(stages) is False


def test_edd_rejects_claims_in_allowed_location_fields():
    from trip_assistant.providers.plan_guard import guard_itinerary

    safe = guard_itinerary({"itinerary": {"daily_plans": [{"activities": []}]}}, [])
    safe["itinerary"]["daily_plans"][0]["activities"] = [
        {"location": "故宫门票两百块，现有库存充足"}
    ]
    assert (
        sourced_output_valid([{"agent_name": "itinerary_planning", "content": {"data": safe}}])
        is False
    )
