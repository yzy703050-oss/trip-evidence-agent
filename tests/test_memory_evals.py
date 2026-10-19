"""Offline regression scenarios grouped by responsibility."""

import json

import pytest

from evals.v0_memory.runner import evaluate_case, main
from trip_assistant.storage.sessions import SessionStore

# v0_memory_evals


def _store(
    tmp_path, *, planned=("rag_knowledge",), actual=("rag_knowledge",), answer="北京住宿上限500元"
):
    store = SessionStore(tmp_path, "alice", "s1")
    store.append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "北京住宿标准？",
        }
    )
    store.append_run(
        {
            "type": "agent_plan",
            "turn_id": "t1",
            "agents": [{"agent_name": name, "priority": 1} for name in planned],
        }
    )
    for name in actual:
        store.append(
            {
                "turn_id": "t1",
                "scope": "run",
                "type": "stage_complete",
                "agent_name": name,
                "priority": 1,
                "status": "success",
                "content": {
                    "status": "success",
                    "data": {
                        "answer": answer,
                        "retrieved_documents": [
                            {
                                "content": "北京住宿上限500元",
                                "metadata": {"parent_doc": "01_travel_standards.txt"},
                            }
                        ],
                    },
                },
            }
        )
    store.append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "assistant",
            "content": "已查询",
            "final": True,
        }
    )
    store.append_run(
        {
            "type": "model_call",
            "stage": "intent",
            "status": "success",
            "latency_ms": 20,
            "input_tokens": 100,
            "output_tokens": 20,
            "estimated_cost_usd": None,
        }
    )
    store.append_run(
        {"type": "query_run", "turn_id": "t1", "status": "completed", "latency_ms": 80}
    )
    return store


def test_eval_catches_misrouted_or_missing_agent_and_reports_real_metrics(tmp_path):
    store = _store(tmp_path, planned=("rag_knowledge",), actual=("information_query",))
    result = evaluate_case({"id": "route", "expected_agents": ["rag_knowledge"]}, store)

    assert result["checks"]["planned_route"] is True
    assert result["checks"]["executed_route"] is False
    assert result["checks"]["plan_execution_match"] is False
    assert result["metrics"]["model_calls"] == 1
    assert result["metrics"]["latency_ms"] == 80
    assert result["metrics"]["input_tokens"] == 100
    assert result["metrics"]["estimated_cost_usd"] is None
    assert result["passed"] is False


def test_eval_checks_memory_fact_rag_source_and_supported_answer(tmp_path):
    store = _store(tmp_path)
    case = {
        "id": "beijing",
        "expected_agents": ["rag_knowledge"],
        "memory_facts": ["全季"],
        "rag_gold_sources": ["01_travel_standards.txt"],
        "answer_facts": ["500元"],
    }
    result = evaluate_case(case, store)

    assert result["checks"]["memory_recall"] is False
    assert result["checks"]["rag_recall_at_3"] is True
    assert result["checks"]["answer_facts"] is True
    assert result["checks"]["rag_grounding"] is True
    assert result["passed"] is False


def test_eval_missing_metrics_are_unknown_and_rag_miss_is_visible(tmp_path):
    store = _store(tmp_path, answer="凭空声称住宿上限800元")
    result = evaluate_case(
        {
            "id": "grounding",
            "expected_agents": ["rag_knowledge"],
            "rag_gold_sources": ["missing.txt"],
            "answer_facts": ["800元"],
        },
        store,
    )

    assert result["checks"]["rag_recall_at_3"] is False
    assert result["checks"]["rag_grounding"] is False
    assert result["metrics"]["estimated_cost_usd"] is None
    assert result["metrics"]["output_tokens"] == 20


def test_fact_match_ignores_spacing_between_number_and_unit(tmp_path):
    store = _store(tmp_path, answer="北京住宿上限 500 元")
    result = evaluate_case(
        {
            "id": "spacing",
            "expected_agents": ["rag_knowledge"],
            "rag_gold_sources": ["01_travel_standards.txt"],
            "answer_facts": ["500元"],
        },
        store,
    )
    assert result["checks"]["answer_facts"] is True
    assert result["checks"]["rag_grounding"] is True


def test_itinerary_eval_distinguishes_feasible_and_worst_case_budget(tmp_path):
    store = SessionStore(tmp_path, "alice", "trip")
    store.append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "预算3000元",
        }
    )
    store.append_run(
        {
            "type": "agent_plan",
            "turn_id": "t1",
            "agents": [{"agent_name": "itinerary_planning", "priority": 2}],
        }
    )
    store.append(
        {
            "turn_id": "t1",
            "scope": "run",
            "type": "stage_complete",
            "agent_name": "itinerary_planning",
            "priority": 2,
            "status": "success",
            "content": {
                "status": "success",
                "data": {
                    "itinerary": {
                        "title": "全季北京三日行程",
                        "daily_plans": [{"day": 1}, {"day": 2}, {"day": 3}],
                        "estimated_budget": (
                            "约2600-3000元：高铁约1100元，酒店约800-1000元，"
                            "餐饮约400-600元，交通约120-180元，其他约100-200元。"
                        ),
                    }
                },
            },
        }
    )
    store.append_run(
        {"type": "query_run", "turn_id": "t1", "status": "completed", "latency_ms": 20}
    )

    result = evaluate_case(
        {
            "id": "trip_budget",
            "expected_agents": ["itinerary_planning"],
            "itinerary_days": 3,
            "itinerary_hotel_brand": "全季",
            "budget_limit_yuan": 3000,
        },
        store,
    )
    assert result["checks"]["itinerary_days"] is True
    assert result["checks"]["itinerary_hotel_brand"] is True
    assert result["checks"]["budget_feasible_within_limit"] is False
    assert result["checks"]["budget_components_within_limit"] is False
    assert result["metrics"]["budget_component_lower_yuan"] is None
    assert result["metrics"]["budget_component_upper_yuan"] is None
    assert result["passed"] is False


def test_optional_quality_judge_receives_question_answer_and_evidence(tmp_path):
    store = _store(tmp_path)
    observed = []

    def judge(payload):
        observed.append(payload)
        return {"passed": True, "reason": "回答与材料一致"}

    result = evaluate_case(
        {"id": "judge", "expected_agents": ["rag_knowledge"]}, store, judge=judge
    )
    assert result["checks"]["model_judge"] is True
    assert observed[0]["question"] == "北京住宿标准？"
    assert "500元" in observed[0]["answer"]
    assert (
        observed[0]["retrieved_documents"][0]["metadata"]["parent_doc"] == "01_travel_standards.txt"
    )
    assert result["judge_reason"] == "回答与材料一致"


def test_eval_command_emits_machine_readable_report(tmp_path, monkeypatch, capsys):
    _store(tmp_path)
    case_file = tmp_path / "cases.json"
    case_file.write_text('{"id":"ok","expected_agents":["rag_knowledge"]}', encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "eval",
            "--cases",
            str(case_file),
            "--storage-root",
            str(tmp_path),
            "--user-id",
            "alice",
            "--session-id",
            "s1",
        ],
    )

    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is True
    assert report["cases"][0]["metrics"]["model_calls"] == 1


def test_eval_only_counts_model_calls_for_selected_turn(tmp_path):
    store = _store(tmp_path)
    store.append(
        {
            "turn_id": "t2",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "下一题",
        }
    )
    store.append_run({"type": "agent_plan", "turn_id": "t2", "agents": []})
    store.append_run(
        {
            "type": "model_call",
            "stage": "intent",
            "status": "success",
            "latency_ms": 10,
            "input_tokens": 50,
            "output_tokens": 10,
            "estimated_cost_usd": None,
        }
    )
    store.append_run(
        {"type": "query_run", "turn_id": "t2", "status": "completed", "latency_ms": 30}
    )

    first = evaluate_case(
        {"id": "first", "turn_id": "t1", "expected_agents": ["rag_knowledge"]}, store
    )
    second = evaluate_case({"id": "second", "turn_id": "t2", "expected_agents": []}, store)
    assert first["metrics"]["model_calls"] == 1
    assert second["metrics"]["model_calls"] == 1
    assert second["metrics"]["input_tokens"] == 50


# compact_intent_evals


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
    from evals.legacy_reports import assess_intents

    assert not assess_intents("hotel_replace", [response(value)])["passed"]


def test_combined_request_requires_both_purposes_and_preference_action():
    from evals.legacy_reports import assess_intents

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
    from evals.legacy_reports import assess_intents

    value = dict(intents=[{"type": kind}], response_mode="direct", agent_schedule=[])
    assert assess_intents(case, [response(value)])["passed"]
    value.update(response_mode="workflow", resume_workflow_id="w1")
    assert not assess_intents(case, [response(value)])["passed"]


def test_repaired_initial_decision_is_checked_but_later_steps_cannot_hide_failure():
    from evals.legacy_reports import assess_intents

    records = [
        response({"intents": [{"type": "replace_hotel"}]}),
        response({"intents": [{"type": "update"}]}, "main:repair_decision"),
    ]
    assert assess_intents("hotel_replace", records)["passed"]
    assert not assess_intents(
        "hotel_replace", [records[0], response({"intents": [{"type": "update"}]}, "main:step")]
    )["passed"]


def test_condition_change_case_changes_the_actual_previous_stay():
    from evals.legacy_reports import case_query

    previous = {"workflow": {"tasks": [{"conditions": {"nights": 3}}]}}
    assert "4晚" in case_query("change_stay", "改为3晚", previous)
    assert case_query("explain", "解释原因", previous) == "解释原因"


def test_memory_read_accepts_known_context_but_never_preference_write():
    from types import SimpleNamespace

    from evals.legacy_reports import assess, assess_intents

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
