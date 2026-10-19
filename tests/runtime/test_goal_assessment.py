from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.state import ExecutionState
from trip_assistant.tools.trip_write import build_trip_writer


def assessment(status="satisfied"):
    return {"status": status, "reason": "对照原始请求与实际安排判断；替代方案不等于原目标完成。"}


async def prepared_selection(runtime):
    from test_review_handoff import proposal

    from trip_assistant.runtime.limits import TurnBudget

    written = await runtime.trip_service.apply(proposal(), {"operation_id": "seed"})
    queried = await runtime.trip_queries.run(
        written["trip_plan_id"],
        written["revision"],
        {"operation_id": "query", "budget": TurnBudget(runtime.settings)},
    )
    trip = queried["trip"]
    view = trip["candidates"][0]
    return {
        "operation": "select",
        "trip_plan_id": trip["trip_plan_id"],
        "expected_revision": trip["revision"],
        "tasks": [
            {
                "task_id": trip["tasks"][0]["id"],
                "selection_refs": {
                    "train": {
                        "query_id": view["query_id"],
                        "result_revision": view["result_revision"],
                        "candidate_id": view["items"][0]["id"],
                    }
                },
            }
        ],
    }


@pytest.mark.parametrize("status", ["partial", "unmet", "unknown"])
async def test_legacy_non_satisfied_label_does_not_control_saving(tmp_path, status):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    result = await build_trip_writer(runtime.trip_service)(
        {"tripplan": plan, "goal_assessment": assessment(status)}, {"operation_id": "select"}
    )
    saved = runtime.memory.workflow_store.load(plan["trip_plan_id"])
    assert result["status"] == "completed"
    assert "goal_assessment" not in result["validation"]
    assert saved["committed"]


async def test_native_missing_assessment_does_not_require_a_repair_call(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    result = await build_trip_writer(runtime.trip_service)(
        {"tripplan": plan}, {"operation_id": "select"}
    )
    assert result["status"] == "completed"
    assert not any(i["code"] == "goal_assessment_required" for i in result["validation"]["issues"])
    assert runtime.memory.workflow_store.load(plan["trip_plan_id"])["committed"]


async def test_native_satisfied_and_legacy_direct_service_can_commit(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    result = await build_trip_writer(runtime.trip_service)(
        {"tripplan": plan, "goal_assessment": assessment()}, {"operation_id": "select"}
    )
    assert result["status"] == "completed"
    assert "goal_assessment" not in result["validation"]
    legacy = await runtime.trip_service.apply(
        {
            "operation": "amend",
            "trip_plan_id": result["trip_plan_id"],
            "expected_revision": result["revision"],
        },
        {"operation_id": "legacy"},
    )
    assert legacy["status"] == "completed"


@pytest.mark.parametrize("new_assessment", [None, assessment("unmet")])
async def test_no_change_amend_preserves_version_without_requiring_a_label(
    tmp_path, new_assessment
):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    writer = build_trip_writer(runtime.trip_service)
    first = await writer(
        {"tripplan": plan, "goal_assessment": assessment()}, {"operation_id": "select"}
    )
    old = deepcopy(runtime.memory.workflow_store.load(plan["trip_plan_id"])["committed"])
    result = await writer(
        {
            "tripplan": {
                "operation": "amend",
                "trip_plan_id": first["trip_plan_id"],
                "expected_revision": first["revision"],
            },
            "goal_assessment": new_assessment,
        },
        {"operation_id": "reevaluate"},
    )
    saved = runtime.memory.workflow_store.load(plan["trip_plan_id"])
    assert result["status"] == "completed"
    assert saved["committed"] == old
    assert saved["revision"] == first["revision"]


async def test_satisfied_never_bypasses_departure_in_past(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    with pytest.raises(ValueError, match="earlier than current time"):
        await build_trip_writer(runtime.trip_service)(
            {"tripplan": plan, "goal_assessment": assessment()},
            {"operation_id": "select", "current_time": "2026-10-20T21:00:00+08:00"},
        )
    assert runtime.memory.workflow_store.load(plan["trip_plan_id"])["committed"] is None


def terminal_pair():
    call = {
        "id": "s",
        "name": "write_trip",
        "arguments": {
            "tripplan": {"operation": "select"},
            "goal_assessment": assessment("unmet"),
            "final_answer": "明天上午到不了，保留后天游玩的替代草案，原目标未完成。",
        },
    }
    result = {
        "call_id": "s",
        "name": "write_trip",
        "operation": "select",
        "status": "partial",
        "trip_plan_id": "trip",
        "revision": 2,
        "stop_planning": True,
        "stop_reason": "goal_not_satisfied",
        "validation": {
            "valid": False,
            "goal_assessment": assessment("unmet"),
            "issues": [{"code": "goal_not_satisfied", "blocking": True}],
        },
    }
    return call, result


def test_explicit_goal_partial_releases_same_answer_without_another_call():
    call, result = terminal_pair()
    assert ExecutionState.ready_reply([call], [result]) == call["arguments"]["final_answer"]


def test_normalized_reason_does_not_force_another_model_call():
    call, result = terminal_pair()
    call["arguments"]["goal_assessment"]["reason"] = "  " + assessment("unmet")["reason"] + "\n"
    assert ExecutionState.ready_reply([call], [result]) == call["arguments"]["final_answer"]


@pytest.mark.parametrize("failure", ["query_pending", "selection_not_ready", "batch_error"])
def test_goal_partial_does_not_hide_other_work_or_errors(failure):
    call, result = terminal_pair()
    batch = [result]
    if failure == "batch_error":
        batch.append({"name": "query", "status": "error", "call_id": "q"})
    else:
        result["validation"]["issues"].append({"code": failure, "blocking": True})
    assert ExecutionState.ready_reply([call], batch) is None


async def test_runtime_legacy_label_does_not_add_another_model_call(tmp_path):
    import json

    from test_review_handoff import call, proposal, response

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})

    def choose(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["original_query"] == "明天上午开始在上海玩"
        trip = payload["tripplan"]
        view = trip["candidates"][0]
        return response(
            call(
                "s",
                "write_trip",
                goal_assessment=assessment("unmet"),
                final_answer="无法赶上明天上午，替代安排已保存，原目标未完成。",
                tripplan={
                    "operation": "select",
                    "trip_plan_id": trip["trip_plan_id"],
                    "expected_revision": trip["revision"],
                    "tasks": [
                        {
                            "task_id": trip["tasks"][0]["id"],
                            "selection_refs": {
                                "train": {
                                    "query_id": view["query_id"],
                                    "result_revision": view["result_revision"],
                                    "candidate_id": view["items"][0]["id"],
                                }
                            },
                        }
                    ],
                },
            )
        )

    runtime.model.script = [response(call("p", "write_trip", tripplan=proposal())), choose]
    result = await runtime.run("明天上午开始在上海玩")
    assert len(runtime.model.messages) == 2
    assert result["status"] == "completed"
    assert result["final_answer"] == "无法赶上明天上午，替代安排已保存，原目标未完成。"
    saved = runtime.memory.workflow_store.list_all()[0]
    assert saved["committed"]
