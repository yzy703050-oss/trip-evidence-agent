import json
from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture
from support.workflow import populated_workflow


def recorded_change(runtime, sessions, identity, trip_id, *, status="completed", revision=2):
    args = {
        "request_kind": "planning",
        "round_summary": "酒店改为安静地点，保留火车",
        "tripplan": {
            "operation": "amend",
            "trip_plan_id": trip_id,
            "expected_revision": revision - 1,
            "reason": "用户希望更安静",
            "tasks": [{"task_id": "task-a", "conditions": {"hotel_keywords": "安静"}}],
        },
    }
    sessions.record_calls([{"id": identity, "name": "write_trip", "arguments": args}], "turn")
    result = {"call_id": identity, "name": "write_trip", "status": status}
    if status == "completed":
        result.update(trip_plan_id=trip_id, revision=revision)
    else:
        result["reason"] = "hard task condition requires user authorization"
    sessions.record_result(identity, result, "turn")


def test_review_context_keeps_all_tasks_and_shared_memory_without_raw_audit(tmp_path):
    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    work = populated_workflow(quoted=True)
    work["tasks"] = [deepcopy(t) for t in work["tasks"] * 2]
    for i, task in enumerate(work["tasks"]):
        task["id"] = "task" + str(i)
    work["raw_payload"] = "不应注入的原始接口报文"
    saved = runtime.trip_service.store.save(work, expected_revision=None)
    recorded_change(runtime, runtime.sessions, "c", saved["id"])
    turn = {"turn_id": "turn", "original_query": "原始需求：只换酒店"}
    payload = review_payload(runtime.sessions, turn, saved["id"])
    assert payload["original_query"] == turn["original_query"]
    snapshot = payload["tripplan"]
    assert len(snapshot["tasks"]) == 6
    assert "confirmed_conditions" in snapshot and "field_sources" in snapshot
    assert all("items" in t for t in snapshot["tasks"])
    assert "results_by_query" not in snapshot and "raw_payload" not in snapshot
    assert "committed" not in snapshot and "working" not in snapshot
    assert payload["previous_round_summary"]["summary"] == "酒店改为安静地点，保留火车"
    assert payload["previous_round_summary"]["results"][0]["status"] == "completed"


def test_trip_adjustments_survive_other_session_compaction_and_keep_failures(tmp_path):
    from trip_assistant.memory.planning import review_payload
    from trip_assistant.storage.sessions import SessionStore

    runtime = NativeRuntimeFixture(tmp_path)
    saved = runtime.trip_service.store.save(populated_workflow(quoted=True), expected_revision=None)
    recorded_change(runtime, runtime.sessions, "first", saved["id"])
    recorded_change(runtime, runtime.sessions, "failed", saved["id"], status="rejected")
    recorded_change(runtime, runtime.sessions, "unrelated", "another-trip")
    before = deepcopy(runtime.sessions.read_events())
    runtime.sessions.update_state({"covered_through_seq": 100, "summary": "旧会话已压缩"})
    other = SessionStore(tmp_path, "a", "other")
    recorded_change(runtime, other, "later", saved["id"], revision=3)
    payload = review_payload(other, {"turn_id": "turn", "original_query": "继续调整"}, saved["id"])
    history = payload["adjustment_history"]
    assert [r["call_id"] for r in history] == ["first", "failed", "later"]
    assert history[1]["effect"] == "not_applied"
    assert history[1]["reason"] == "用户希望更安静"
    assert history[1]["result_reason"].startswith("hard task")
    assert runtime.sessions.read_events() == before
    isolated = SessionStore(tmp_path, "b", "other")
    assert (
        review_payload(isolated, {"turn_id": "turn", "original_query": "查询"}, saved["id"]) is None
    )


def test_recorded_history_does_not_certify_proposed_prices_or_inventory():
    from trip_assistant.presentation.facts import finalize

    checked = finalize(
        "曾调整到2026-10-18，票价100元。",
        [
            {"name": "write_trip", "status": "rejected", "reason": "stale revision"},
        ],
        context={
            "review_evidence": {
                "adjustment_history": [
                    {
                        "changes": {
                            "conditions": {
                                "departure_date": "2026-10-18",
                                "constraints": {"train_max_total_cny": 100},
                            }
                        },
                    }
                ]
            }
        },
    )
    assert checked["execution_facts"]["saved"] is False
    assert "price_cny" not in checked["execution_facts"]
    assert checked["execution_facts"]["errors"][0]["status"] == "rejected"


def test_previous_summary_is_latest_call_in_current_planning_turn(tmp_path):
    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    saved = runtime.trip_service.store.save(populated_workflow(quoted=True), expected_revision=None)
    recorded_change(runtime, runtime.sessions, "first", saved["id"])
    runtime.sessions.record_calls(
        [
            {
                "id": "new",
                "name": "write_trip",
                "arguments": {
                    "request_kind": "planning",
                    "round_summary": "改返程，酒店维持不动",
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": saved["id"],
                        "expected_revision": 1,
                    },
                },
            }
        ],
        "current",
    )
    runtime.sessions.record_result(
        "new",
        {"call_id": "new", "name": "write_trip", "status": "rejected", "reason": "conflict"},
        "current",
    )
    payload = review_payload(
        runtime.sessions, {"turn_id": "current", "original_query": "改返程"}, saved["id"]
    )
    assert payload["previous_round_summary"]["summary"] == "改返程，酒店维持不动"
    assert payload["previous_round_summary"]["results"][0]["reason"] == "conflict"
    assert len(payload["adjustment_history"]) == 2
    assert "tool_calls" not in json.dumps(payload, ensure_ascii=False)


def test_atomic_receipt_keeps_saved_adjustment_when_tool_result_was_interrupted(tmp_path):
    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    saved = runtime.trip_service.store.save(
        populated_workflow(quoted=True), expected_revision=None, operation_id="turn:interrupted"
    )
    runtime.sessions.record_calls(
        [
            {
                "id": "interrupted",
                "name": "write_trip",
                "arguments": {
                    "request_kind": "planning",
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": saved["id"],
                        "expected_revision": 0,
                    },
                    "round_summary": "调整已保存，但调用结果尚未写回",
                },
            }
        ],
        "turn",
    )
    payload = review_payload(
        runtime.sessions, {"turn_id": "turn", "original_query": "继续"}, saved["id"]
    )
    assert payload["adjustment_history"][0]["effect"] == "saved_draft"
    assert payload["adjustment_history"][0]["revision"] == saved["revision"]


def test_business_snapshot_preserves_all_planned_items_with_same_domain(tmp_path):
    from trip_assistant.trips.contracts import TripPlanInput

    runtime = NativeRuntimeFixture(tmp_path)
    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-17"},
                "items": [
                    {"domain": "train", "ranking": [{"field": "price_cny"}]},
                    {"domain": "train", "ranking": [{"field": "duration_minutes"}]},
                ],
            }
        ],
    }
    prepared = runtime.trip_service.preview(TripPlanInput.model_validate(plan).model_dump(), {})
    assert len(prepared["working"]["tasks"][0]["items"]) == 2


@pytest.mark.parametrize("plan,summary", [(None, None), ([], 123), ({"operation": "select"}, 123)])
def test_rejected_malformed_input_can_still_be_summarized(tmp_path, plan, summary):
    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    runtime.sessions.record_calls(
        [
            {
                "id": "bad",
                "name": "write_trip",
                "arguments": {
                    "request_kind": "planning",
                    "tripplan": plan,
                    "round_summary": summary,
                },
            }
        ],
        "turn",
    )
    runtime.sessions.record_result(
        "bad",
        {
            "call_id": "bad",
            "name": "write_trip",
            "status": "rejected",
            "reason": "invalid input",
        },
        "turn",
    )
    payload = review_payload(runtime.sessions, {"turn_id": "turn", "original_query": "规划"})
    assert payload["previous_round_summary"]["results"][0]["reason"] == "invalid input"
    assert payload["previous_round_summary"]["summary"] == ""
    assert payload["tripplan"] == (plan if isinstance(plan, dict) else {})


@pytest.mark.asyncio
async def test_stage_projection_keeps_budget_checks_without_summarizing_raw_history(tmp_path):
    from trip_assistant.llm.budget import ContextBudgetExceeded
    from trip_assistant.llm.messages import ModelTurn
    from trip_assistant.memory.compaction import prepare_context

    runtime = NativeRuntimeFixture(tmp_path)
    runtime.sessions.append(
        {
            "type": "message",
            "role": "user",
            "scope": "session",
            "turn_id": "old",
            "content": "旧信息" * 10000,
        }
    )
    before = runtime.sessions.read_events()
    summaries = []

    async def summarize(previous, events):
        summaries.append(events)
        return "旧摘要"

    options = {
        "prefix": [{"role": "system", "content": "评估"}],
        "tools": [],
        "model": None,
        "budget_tokens": 100,
        "summarizer": summarize,
        "counter": lambda p: sum(len(m.get("content") or "") for m in p["messages"]),
        "projection": lambda: [{"role": "user", "content": "当前行程"}],
    }
    messages = await prepare_context(runtime.sessions, **options)
    assert len(messages) == 2 and summaries == []
    with pytest.raises(ContextBudgetExceeded) as caught:
        await prepare_context(runtime.sessions, **options, response=ModelTurn("答" * 200, []))
    assert caught.value.phase == "output"
    assert runtime.sessions.read_events() == before
