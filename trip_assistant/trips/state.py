"""Small business state enums; waiting, cancellation and in-flight are derived."""

from copy import deepcopy

STOPPED = {"pause", "cancel", "paused", "cancelled"}


def planning_stage(workflow):
    if workflow.get("status") == "completed":
        return "completed"
    if any(
        p["reason"] in {"awaiting_query", "query_in_progress"}
        for p in workflow.get("pending_queries", [])
    ):
        return "awaiting_query"
    return (
        "awaiting_selection" if workflow.get("stop_reason") == "awaiting_model" else "needs_repair"
    )


def invalidate_query_binding(container, reason):
    value = deepcopy(container)
    ref = value.get("current_query_ref")
    history = value.setdefault("history_refs", [])
    if ref and ref not in history:
        history.append(ref)
    value.update(status="empty", current_query_ref=None, invalidation_reason=reason)
    return value


def normalize_workflow_states(workflow, *, check_evidence=True):
    value = deepcopy(workflow)
    old = value.get("status", "running")
    if old not in {
        "running",
        "completed",
        "needs_input",
        "partial",
        "error",
        "paused",
        "cancelled",
    }:
        raise ValueError("unknown workflow state")
    if old != "completed":
        value["status"] = "running"
        if old != "running" and not value.get("stop_reason"):
            value["stop_reason"] = old
    for task in value.get("tasks", []):
        task.setdefault("content", [])
        status = task.get("status") or "pending"
        if status not in {"pending", "draft", "validated", "running", "needs_input"}:
            raise ValueError("unknown task state")
        task["status"] = "pending" if status in {"running", "needs_input"} else status
        if task["status"] == "draft":
            task.setdefault("draft_stage", "selection" if task.get("draft_plan") else "query")
        containers = task.get("query_containers", {})
        for domain, container in containers.items():
            state = container.get("status", "empty")
            if state == "querying":
                request_id = (
                    container.get("request_id")
                    or f"legacy:{task['id']}:{domain}:{task['revision']}"
                )
                records = value.setdefault("execution_plan", [])
                if not any(r.get("request_id") == request_id for r in records):
                    records.append(
                        {
                            "request_id": request_id,
                            "task_id": task["id"],
                            "task_revision": task["revision"],
                            "domain": domain,
                            "started_at": container.get("started_at") or "legacy-unknown",
                            "completed_at": container.get("completed_at"),
                        }
                    )
            if state in {"stale", "blocked", "querying"}:
                task["query_containers"][domain] = invalidate_query_binding(container, state)
            elif state == "faild":
                container["status"] = "failed"
            elif state not in {"empty", "ready", "failed"}:
                raise ValueError("unknown container state")
            elif state == "ready":
                row = value.get("results_by_query", {}).get(container.get("current_query_ref"))
                if (
                    not row
                    or row.get("task_revision") != task.get("revision")
                    or row.get("needs_revalidation")
                ):
                    task["query_containers"][domain] = invalidate_query_binding(
                        container, "binding_invalid"
                    )
    if check_evidence and (
        old == "completed" or any(t["status"] == "validated" for t in value.get("tasks", []))
    ):
        from trip_assistant.trips.validation import check_task, check_workflow

        for task in value.get("tasks", []):
            if (
                task["status"] == "validated"
                and not check_task(value, task["id"], task.get("draft_plan"))["valid"]
            ):
                task["status"] = "draft" if task.get("draft_plan") else "pending"
                if task["status"] == "draft":
                    task["draft_stage"] = "selection"
        if old == "completed":
            check = check_workflow(value)
            if not check["valid"]:
                value.update(status="running", stop_reason="validation_required", validation=check)
    return value


def derive_dispatchability(workflow, task_id, execution_records):
    task = next(t for t in workflow["tasks"] if t["id"] == task_id)
    stopped = workflow.get("stop_reason") in STOPPED
    inflight = [
        r["request_id"]
        for r in execution_records
        if r.get("task_id") == task_id
        and r.get("task_revision") == task["revision"]
        and r.get("started_at")
        and not r.get("completed_at")
    ]
    pending = {t["id"] for t in workflow["tasks"] if t["status"] == "pending"}
    dependencies = [d for d in task.get("depends_on", []) if d in pending]
    missing = []
    if not task.get("origin") and "train" in task.get("update_scope", ["train"]):
        missing.append("origin")
    checkpoint = workflow.get("checkpoint") or {}
    if task_id in checkpoint.get("affected_task_ids", []):
        missing.extend(checkpoint.get("missing_fields", []))
    return {
        "executable": not (stopped or inflight or dependencies or missing),
        "stopped": stopped,
        "inflight": inflight,
        "waiting_dependencies": dependencies,
        "missing_fields": sorted(set(missing)),
    }
