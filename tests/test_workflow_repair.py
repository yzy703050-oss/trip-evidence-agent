from copy import deepcopy
import pytest
from agents.workflow_contracts import validate_action, apply_user_update
from agents.workflow_guard import check_workflow
from workflow_support import populated_workflow


def conflict():
    w = populated_workflow()
    t = w["tasks"][1]
    t["conditions"]["departure_date"] = "2026-10-12"
    t["field_sources"]["departure_date"] = "proposal"
    row = w["results_by_query"][t["draft_plan"]["train_selection"]["query_id"]]
    row["parameters"]["departure_date"] = "2026-10-12"
    for item in row["items"]:
        for k in ("departure_at", "arrival_at"):
            item[k] = item[k].replace("2026-10-13", "2026-10-12")
    assert "departure_before_previous_checkout" in [i["code"] for i in check_workflow(w)["issues"]]
    return w


def action(w, index=1, components=None):
    return dict(
        action="repair_tasks",
        workflow_revision=w["revision"],
        task_ids=[w["tasks"][index]["id"]],
        components=components or ["train"],
        conflict_codes=["departure_before_previous_checkout"],
        reason="调整衔接",
    )


def test_old_task_can_be_selected_but_unrelated_stale_or_condition_writes_rejected():
    w = conflict()
    assert validate_action(action(w), w)["task_ids"] == [w["tasks"][1]["id"]]
    for bad in [
        action(w, 2),
        {**action(w), "workflow_revision": w["revision"] - 1},
        {**action(w), "condition_updates": {"nights": 1}},
        {**action(w), "task_ids": [w["tasks"][1]["id"]] * 2},
    ]:
        with pytest.raises(ValueError):
            validate_action(bad, w)


def test_repair_keeps_hard_dates_and_unmodified_hotel_but_invalidates_train():
    from agents.workflow_repair import apply_repair

    w = conflict()
    original = deepcopy(w)
    t = w["tasks"][1]
    repaired = apply_repair(w, action(w))
    root = repaired["tasks"][1]
    assert w == original
    assert root["revision"] == t["revision"] + 1 and root["status"] == "pending"
    assert root["conditions"]["check_in"] == t["conditions"]["check_in"]
    assert "departure_date" not in root["conditions"]
    assert root["draft_plan"]["train_selection"] is None
    assert root["draft_plan"]["hotel_selection"] == t["draft_plan"]["hotel_selection"]
    hotel_row = repaired["results_by_query"][root["draft_plan"]["hotel_selection"]["query_id"]]
    assert hotel_row["task_revision"] == root["revision"]
    assert repaired["checkpoint"]["kind"] == "task_repair"
    assert repaired["tasks"][0] == original["tasks"][0]
    assert (
        repaired["results_by_query"][t["draft_plan"]["train_selection"]["query_id"]][
            "task_revision"
        ]
        == t["revision"]
    )
    resumed = apply_user_update(repaired, {})
    assert resumed["checkpoint"] == repaired["checkpoint"]


def test_repair_with_unchanged_boundary_preserves_downstream():
    from agents.workflow_repair import apply_repair, advance_repair

    w = conflict()
    original = deepcopy(w)
    repaired = apply_repair(w, action(w))
    root = repaired["tasks"][1]
    root["draft_plan"] = deepcopy(w["tasks"][1]["draft_plan"])
    root["draft_plan"]["task_revision"] = root["revision"]
    q = repaired["results_by_query"][root["draft_plan"]["train_selection"]["query_id"]]
    q["task_revision"] = root["revision"]
    root["status"] = "draft"
    advance_repair(repaired, root["id"])
    assert repaired["tasks"][2] == original["tasks"][2]
    assert repaired["checkpoint"] is None
    assert not check_workflow(repaired)["valid"]  # A repair never bypasses the real conflict.


def test_changed_root_boundary_reopens_only_invalid_dependent():
    from agents.workflow_repair import apply_repair, advance_repair

    w = conflict()
    repaired = apply_repair(w, action(w, 0, ["hotel", "schedule"]))
    root = repaired["tasks"][0]
    root["draft_plan"]["schedule"]["check_out"] = "2026-10-14"
    root["status"] = "draft"
    # Preserve user check-out: rejected as inconsistent; dependent must still be rechecked.
    advance_repair(repaired, root["id"])
    assert repaired["tasks"][1]["status"] == "pending"
    assert repaired["tasks"][1]["needs_dependency_check"]
    assert repaired["checkpoint"]["kind"] == "task_repair"


@pytest.mark.asyncio
async def test_runner_repairs_old_middle_task_without_querying_unchanged_components(tmp_path):
    from workflow_runtime import Runtime, WorkflowMain
    from agents.contracts import RunState
    from agents.workflow_runner import WorkflowRunner

    class RepairMain(WorkflowMain):
        async def step(self, context):
            w, t = context["workflow"], context["current_task"]
            if w.get("validation") and not w["validation"]["valid"]:
                return action(w)
            if t["status"] == "pending":
                requests = [
                    r
                    for r in context["preflight"]["query_requests"]
                    if r["domain"] in t.get("update_scope", ["train", "hotel"])
                ]
                return {
                    "action": "dispatch",
                    "task_id": t["id"],
                    "goal": "修复当前段",
                    "query_requests": requests,
                }
            decision = await super().step(context)
            if decision["action"] == "draft_task" and "hotel" not in t.get(
                "update_scope", ["train", "hotel"]
            ):
                decision["draft_plan"]["hotel_selection"] = deepcopy(
                    t["draft_plan"]["hotel_selection"]
                )
            return decision

    runtime = Runtime(tmp_path, main=RepairMain())
    w = conflict()
    w["validation"] = check_workflow(w)
    w["current_task_id"] = w["tasks"][-1]["id"]
    saved = runtime.memory.workflow_store.save(w, expected_revision=None)
    run = RunState(runtime.memory.start_turn("继续修复"))
    result = await WorkflowRunner(runtime.main, runtime.info, runtime.memory).run(
        {"original_query": "继续修复", "current_time": "2026-10-10T10:00:00+08:00"}, run, saved
    )
    assert result["status"] == "completed", result
    assert len(runtime.train.calls) == 1 and not runtime.hotel.calls
    assert result["workflow"]["tasks"][1]["revision"] == 2
    for index in (0, 2):
        assert result["workflow"]["tasks"][index]["revision"] == saved["tasks"][index]["revision"]
        assert (
            result["workflow"]["tasks"][index]["draft_plan"] == saved["tasks"][index]["draft_plan"]
        )
    assert result["workflow"]["checkpoint"] is None


def test_repeat_repair_without_fact_change_is_rejected():
    from agents.workflow_repair import apply_repair

    w = conflict()
    repaired = apply_repair(w, action(w))
    repaired["tasks"] = deepcopy(w["tasks"])
    with pytest.raises(ValueError, match="repair_no_progress"):
        apply_repair(repaired, action(repaired))


def test_invalid_train_reference_does_not_clear_valid_hotel_evidence():
    from agents.workflow_repair import apply_repair

    w = conflict()
    t = w["tasks"][1]
    t["draft_plan"]["train_selection"]["candidate_id"] = "not-saved"
    request = action(w)
    request["conflict_codes"] = ["invalid_candidate_reference"]
    repaired = apply_repair(w, request)
    assert repaired["tasks"][1]["update_scope"] == ["train"]
    assert (
        repaired["tasks"][1]["draft_plan"]["hotel_selection"] == t["draft_plan"]["hotel_selection"]
    )


@pytest.mark.asyncio
async def test_saved_repair_checkpoint_resumes_after_scheduling_stop(tmp_path):
    from workflow_runtime import Runtime, WorkflowMain
    from agents.contracts import RunState
    from agents.workflow_runner import WorkflowRunner, WorkflowLimits

    class Main(WorkflowMain):
        async def step(self, context):
            w, t = context["workflow"], context["current_task"]
            if w.get("validation") and not w["validation"]["valid"]:
                return action(w)
            if t["status"] == "pending":
                return dict(
                    action="dispatch",
                    task_id=t["id"],
                    goal="继续修复",
                    query_requests=context["preflight"]["query_requests"],
                )
            decision = await super().step(context)
            if decision["action"] == "draft_task" and "hotel" not in t.get(
                "update_scope", ["train", "hotel"]
            ):
                decision["draft_plan"]["hotel_selection"] = deepcopy(
                    t["draft_plan"]["hotel_selection"]
                )
            return decision

    rt = Runtime(tmp_path, main=Main())
    w = conflict()
    w["validation"] = check_workflow(w)
    w["current_task_id"] = w["tasks"][-1]["id"]
    saved = rt.memory.workflow_store.save(w, expected_revision=None)
    run = RunState(rt.memory.start_turn("继续修复"))
    first = await WorkflowRunner(
        rt.main, rt.info, rt.memory, limits=WorkflowLimits(main_decisions_per_turn=2)
    ).run({"original_query": "继续修复", "current_time": "2026-10-10T10:00:00+08:00"}, run, saved)
    assert first["stop_reason"] == "main_decision_limit"
    assert first["workflow"]["checkpoint"]["kind"] == "task_repair"
    resumed = Runtime(tmp_path, main=Main(), session="next")
    resumed.main.resume = {"id": saved["id"], "update": {}}
    final = await resumed.turn("继续修复旅程")
    assert final["status"] == "completed", final
    assert not resumed.train.calls and not resumed.hotel.calls
    assert final["workflow"]["checkpoint"] is None


def test_updated_price_evidence_is_business_progress_despite_same_candidate_id():
    from agents.workflow_repair import apply_repair

    w = populated_workflow(quoted=True)
    w["confirmed_conditions"]["constraints"] = {"total_budget_cny": "600"}
    request = action(w, components=["hotel"])
    request["conflict_codes"] = ["total_budget_exceeded"]
    repaired = apply_repair(w, request)
    repaired["tasks"] = deepcopy(w["tasks"])
    repaired["results_by_query"] = deepcopy(w["results_by_query"])
    ref = w["tasks"][1]["draft_plan"]["hotel_selection"]
    repaired["results_by_query"][ref["query_id"]]["items"][0]["stay_total_cny"] = "150"
    assert not check_workflow(repaired)["valid"]
    assert apply_repair(repaired, request)["checkpoint"]["kind"] == "task_repair"
