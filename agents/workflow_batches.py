"""Bind model proposals to program-owned task identity and query containers."""

from copy import deepcopy
from agents.workflow_contracts import task_by_id, identifier, effective_conditions
from agents.workflow_state import invalidate_query_binding
from agents.planning_conditions import prepare_task

TOOLS = {
    "train": "train_search",
    "hotel": "hotel_search",
    "weather": "weather_query",
    "guide": "travel_guide",
    "web": "web_search",
}


def initialize_query_containers(workflow):
    for task in workflow["tasks"]:
        containers = task.setdefault("query_containers", {})
        for domain in ["train", *(["hotel"] if task["requires_hotel"] else [])]:
            if domain not in containers:
                containers[domain] = {
                    "domain": domain,
                    "status": "empty",
                    "parameters": {},
                    "current_query_ref": None,
                    "history_refs": [],
                    "invalidation_reason": None,
                }
                rows = [
                    r
                    for r in workflow.get("results_by_query", {}).values()
                    if r["task_id"] == task["id"]
                    and r["domain"] == domain
                    and r["task_revision"] == task["revision"]
                    and not r.get("needs_revalidation")
                ]
                selected = (task.get("draft_plan") or {}).get(domain + "_selection") or {}
                current = next(
                    (r for r in rows if r["id"] == selected.get("query_id")),
                    rows[-1] if rows else None,
                )
                if current:
                    containers[domain].update(
                        status="ready" if current["status"] in {"ok", "partial"} else "failed",
                        parameters=deepcopy(current["parameters"]),
                        current_query_ref=current["id"],
                    )
    return workflow


def bind_query_plan(query_plan, workflow, context):
    if not isinstance(query_plan, list):
        raise ValueError("query plan must be a list")
    requests, gaps = [], []
    for proposed in query_plan:
        row = deepcopy(proposed)
        dependencies = row.get("depends_on", [])
        if not isinstance(dependencies, list) or not all(
            isinstance(d, str) and d for d in dependencies
        ):
            raise ValueError("invalid query dependency identities")
        domain = row.get("domain")
        if domain not in TOOLS:
            raise ValueError("query domain not enabled")
        parameters = row.get("parameters", {})
        default_name = TOOLS[domain]
        if not isinstance(parameters, dict):
            raise ValueError("invalid query parameters")
        if workflow is not None:
            if domain not in {"train", "hotel", "weather"}:
                raise ValueError("domain outside workflow scope")
            initialize_query_containers(workflow)
            if "task_index" in row:
                index = row["task_index"]
                if type(index) is not int or not 0 <= index < len(workflow["tasks"]):
                    raise ValueError("invalid task index")
                task = workflow["tasks"][index]
            else:
                task = task_by_id(workflow, row.get("task_id"))
                if row.get("task_revision") != task["revision"]:
                    raise ValueError("stale query plan")
            if domain == "hotel" and not task["requires_hotel"]:
                raise ValueError("hotel outside authorized scope")
            # Query-plan suggestions may fill absent dates, but never overwrite conditions.
            for key in ("departure_date", "arrival_date", "check_in", "check_out"):
                if (
                    key in parameters
                    and not task["conditions"].get(key)
                    and not workflow.get("confirmed_conditions", {}).get(key)
                ):
                    from agents.workflow_contracts import validate_conditions

                    validate_conditions({key: parameters[key]})
                    task["conditions"][key] = parameters[key]
                    task["field_sources"][key] = "proposal"
            prepared = prepare_task(workflow, task["id"], context.get("current_time"))
            expected = next((q for q in prepared["query_requests"] if q["domain"] == domain), None)
            effective, _ = effective_conditions(
                workflow.get("confirmed_conditions", {}), task["conditions"], {}
            )
            fixed = {"origin": task.get("origin"), "destination": task.get("destination")}
            fixed.update({k: v for k, v in effective.items() if k != "constraints"})
            if domain == "hotel":
                fixed["city"] = task["destination"]
            if any(
                k in fixed and fixed[k] is not None and v != fixed[k] for k, v in parameters.items()
            ):
                raise ValueError("query changes confirmed conditions")
            if expected is not None:
                parameters = {**expected.get("parameters", {}), **parameters}
                if domain == "train" and prepared["query_mode"] == "arrival":
                    default_name = "train_search_by_arrival"
            if domain != "weather" and expected is None:
                gaps.append(
                    {
                        "task_id": task["id"],
                        "domain": domain,
                        "missing_fields": prepared["missing_fields"],
                    }
                )
                continue
            row.update(task_id=task["id"], task_revision=task["revision"])
        row.update(
            request_id=row.get("request_id") or identifier("request"),
            domain=domain,
            name=row.get("name") or default_name,
            parameters=parameters,
        )
        if row["name"] not in {
            TOOLS[domain],
            "train_search_by_arrival" if domain == "train" else TOOLS[domain],
        }:
            raise ValueError("tool/domain mismatch")
        requests.append(row)
    return {"requests": requests, "gaps": gaps}


def mark_query_draft(workflow, task_id):
    task = task_by_id(workflow, task_id)
    if all(c["status"] == "ready" for c in task.get("query_containers", {}).values()):
        task.update(status="draft", draft_stage="query")
    return task


def invalidate_query_containers(workflow, task_ids, components, condition_changes):
    for identity in task_ids:
        task = task_by_id(workflow, identity)
        for domain, container in task.get("query_containers", {}).items():
            if domain in components or "schedule" in components:
                task["query_containers"][domain] = invalidate_query_binding(
                    container, "conditions_changed"
                )
    return workflow


def check_batch_submission(workflow, action):
    from agents.workflow_guard import check_task, check_workflow, finalize_workflow
    from agents.condition_strengths import condition_is_hard

    if action.get("expected_workflow_revision") != workflow["revision"]:
        raise ValueError("stale batch revision")
    candidate = deepcopy(workflow)
    rows = action.get("draft_tasks")
    if not isinstance(rows, list) or len({r.get("task_id") for r in rows}) != len(rows):
        raise ValueError("invalid batch tasks")
    for row in rows:
        task = task_by_id(candidate, row.get("task_id"))
        if row.get("task_revision") != task["revision"]:
            raise ValueError("stale batch task")
        checked = check_task(candidate, task["id"], row.get("draft_plan"))
        if any(
            i["code"]
            in {
                "invalid_draft",
                "invalid_candidate_reference",
                "stale_draft",
                "preserved_component_changed",
            }
            for i in checked["issues"]
        ):
            raise ValueError("invalid batch draft/reference")
        task.update(
            status="draft",
            draft_stage="selection",
            draft_plan=deepcopy(row["draft_plan"]),
            issues=checked["issues"],
        )
        departure = checked["reconstructed_plan"]["schedule"].get("departure_at")
        if departure and not task["conditions"].get("departure_date"):
            task["conditions"]["departure_date"] = departure[:10]
            task["field_sources"]["departure_date"] = "derived"
        # Model-selected stay suggestions become the next segment's boundary.
        # User/context dates stay fixed, and quoted stay dates must match evidence.
        for key in ("check_in", "check_out"):
            proposed = checked["reconstructed_plan"]["schedule"].get(key)
            if proposed and not condition_is_hard(candidate, task, key):
                task["conditions"][key] = proposed
                task["field_sources"][key] = "derived"
    checked = check_workflow(candidate)
    candidate["validation"] = checked
    if checked["valid"]:
        candidate = finalize_workflow(candidate, checked)
        candidate.pop("refresh_scope", None)
    return {"workflow": candidate, "validation": checked}
