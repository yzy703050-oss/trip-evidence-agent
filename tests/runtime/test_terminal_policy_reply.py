from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.state import ExecutionState


def selection():
    return {
        "id": "s",
        "name": "write_trip",
        "arguments": {
            "tripplan": {"operation": "select"},
            "goal_assessment": {
                "status": "satisfied",
                "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
            },
            "final_answer": "车次已选，企业政策未核实，当前保留草稿。",
        },
    }


def receipt():
    return {
        "call_id": "s",
        "name": "write_trip",
        "operation": "select",
        "status": "partial",
        "trip_plan_id": "trip",
        "revision": 2,
        "stop_planning": True,
        "stop_reason": "policy_unverified",
        "validation": {"valid": False, "issues": [{"code": "policy_unverified", "blocking": True}]},
    }


def test_terminal_policy_gap_reuses_same_answer_without_explicit_disable_flag():
    call = selection()
    assert ExecutionState.ready_reply([call], [receipt()]) == call["arguments"]["final_answer"]


@pytest.mark.parametrize(
    "failure",
    ["query_pending", "selection_not_ready", "other_failure", "not_stopped", "missing_identity"],
)
def test_policy_partial_never_hides_other_unresolved_work(failure):
    result = deepcopy(receipt())
    batch = [result]
    if failure == "other_failure":
        batch.append({"call_id": "q", "name": "query", "status": "error"})
    elif failure == "not_stopped":
        result["stop_planning"] = False
    elif failure == "missing_identity":
        result.pop("trip_plan_id")
    else:
        result["validation"]["issues"].append({"code": failure, "blocking": True})
    assert ExecutionState.ready_reply([selection()], batch) is None


async def test_real_policy_partial_select_finishes_without_fourth_model_call(tmp_path):
    from test_review_handoff import call, proposal, response

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = proposal()
    plan["tasks"][0]["purpose"] = "business"

    def select(messages):
        import json

        trip = json.loads(messages[-1]["content"])["tripplan"]
        view = trip["candidates"][0]
        return response(
            call(
                "s",
                "write_trip",
                goal_assessment={
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                final_answer=selection()["arguments"]["final_answer"],
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
                                },
                            },
                        }
                    ],
                },
            )
        )

    runtime.model.script = [
        response(call("policy", "query_policy")),
        response(call("p", "write_trip", tripplan=plan)),
        select,
    ]
    result = await runtime.run("规划重庆去上海单程出差")
    assert len(runtime.model.messages) == 3
    assert result["status"] == "partial" and result["stop_reason"] == "policy_unverified"
    assert result["final_answer"] == selection()["arguments"]["final_answer"]
    saved = runtime.memory.workflow_store.load(result["trip"]["trip_plan_id"])
    assert saved["committed"] is None and saved["tasks"][0]["draft_plan"]["train_selection"]
