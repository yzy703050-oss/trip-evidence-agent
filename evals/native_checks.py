"""Feasibility checks for current snapshots and recorded reports."""

from trip_assistant.trips.validation import check_workflow, state_signature


def evaluate_result(result, *, expected_tasks=None):
    workflow = result.get("workflow")
    checks = {"response_not_error": result.get("status") != "error"}
    complete = result.get("status") == "completed"
    if expected_tasks is not None:
        checks["workflow_created"] = isinstance(workflow, dict)
        checks["task_count"] = (
            isinstance(workflow, dict) and len(workflow.get("tasks", [])) == expected_tasks
        )
    if workflow:
        try:
            checked = check_workflow(workflow)
            checks["only_train_hotel"] = all(
                r["domain"] in {"train", "hotel"} for r in workflow["results_by_query"].values()
            )
            checks["no_activity_output"] = all(
                "activities" not in (t.get("draft_plan") or {}) for t in workflow["tasks"]
            )
            checks["place_quotes_not_invented"] = all(
                item.get("stay_total_cny") is None
                and item.get("availability", "unknown") == "unknown"
                for row in workflow["results_by_query"].values()
                for item in row["items"]
                if item.get("kind") == "hotel_place"
            )
            if complete:
                checks["current_global_validation"] = (
                    checked["valid"]
                    and bool(workflow.get("validation", {}).get("valid"))
                    and workflow["validation"].get("signature") == state_signature(workflow)
                )
                checks["all_current_tasks_validated"] = all(
                    t["status"] == "validated" and t["draft_plan"]["task_revision"] == t["revision"]
                    for t in workflow["tasks"]
                )
            if result.get("status") == "needs_input":
                checks["checkpoint_saved"] = bool(
                    (workflow.get("checkpoint") or {}).get("question")
                )
        except (ValueError, KeyError, TypeError, AttributeError):
            checks["valid_workflow_contract"] = False
    elif complete:
        checks["completed_needs_workflow"] = False
    rows = [
        row
        for stage in result.get("results", [])
        for row in stage.get("data", {}).get("query_results", [])
    ]
    rows += list(result.get("domain_results", {}).values())
    query_verified = any(
        r.get("items") and r.get("source") and r.get("status") in {"ok", "partial"} for r in rows
    )
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "planning_complete": complete and all(checks.values()),
        "external_query_verified": query_verified,
        "status": result.get("status"),
        "stop_reason": result.get("stop_reason"),
    }
