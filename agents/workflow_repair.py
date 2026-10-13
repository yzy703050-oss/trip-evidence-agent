"""Controlled model-selected repair; durable state remains owned by the runner."""

from copy import deepcopy
import json
import hashlib
from agents.workflow_contracts import task_by_id
from agents.workflow_guard import check_workflow, check_task
from agents.travel_updates import _rebind


def validate_repair(action, workflow):
    if (
        type(action.get("workflow_revision")) is not int
        or action["workflow_revision"] != workflow["revision"]
    ):
        raise ValueError("stale workflow repair")
    ids, components, codes = (action.get(k) for k in ("task_ids", "components", "conflict_codes"))
    for values in (ids, components, codes):
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("repair needs unique task/component/conflict lists")
    if not set(components) <= {"train", "hotel", "schedule"}:
        raise ValueError("invalid repair components")
    if not isinstance(action.get("reason"), str) or not action["reason"].strip():
        raise ValueError("repair needs a reason")
    issues = [i for i in check_workflow(workflow)["issues"] if i["blocking"]]
    if any(code not in {i["code"] for i in issues} for code in codes):
        raise ValueError("repair must reference actual blocking conflicts")
    related = {tid for i in issues if i["code"] in codes for tid in i["task_ids"]}
    for tid in ids:
        task = task_by_id(workflow, tid)
        if tid not in related:
            raise ValueError("unrelated repair task")
        if "hotel" in components and not task["requires_hotel"]:
            raise ValueError("no hotel in repair task")
    return deepcopy(action)


def _fingerprint(workflow, action):
    # Exclude bookkeeping revisions/query IDs so reopening cannot disguise a repeated repair.
    tasks = []
    for t in workflow["tasks"]:
        refs = t.get("draft_plan") or {}
        evidence = {}
        for domain in ("train", "hotel"):
            ref = refs.get(domain + "_selection") or {}
            row = workflow["results_by_query"].get(ref.get("query_id"), {})
            item = next((i for i in row.get("items", []) if i["id"] == ref.get("candidate_id")), {})
            evidence[domain] = {
                "status": row.get("status"),
                "needs_revalidation": row.get("needs_revalidation"),
                "provider": (row.get("source") or {}).get("provider"),
                "facts": {
                    k: item[k]
                    for k in (
                        "id",
                        "kind",
                        "departure_at",
                        "arrival_at",
                        "price_cny",
                        "stay_total_cny",
                        "check_in",
                        "check_out",
                        "guests",
                        "fees_included",
                        "availability",
                        "remaining",
                    )
                    if k in item
                },
            }
        tasks.append(
            {
                "id": t["id"],
                "conditions": t["conditions"],
                "field_sources": t["field_sources"],
                "evidence": evidence,
                "schedule": refs.get("schedule"),
                "selected": {
                    d: (refs.get(d + "_selection") or {}).get("candidate_id")
                    for d in ("train", "hotel")
                },
            }
        )
    payload = json.dumps(
        {
            "tasks": tasks,
            "roots": action["task_ids"],
            "components": action["components"],
            "codes": action["conflict_codes"],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reopen(workflow, task, components):
    domains = set(components) & {"train", "hotel"}
    if "schedule" in components:
        domains.add("train")
        if task["requires_hotel"]:
            domains.add("hotel")
    # Keep unrelated evidence only if that component itself has no blocking defect.
    for domain in {"train", "hotel"} - domains:
        probe = deepcopy(task.get("draft_plan"))
        if probe:
            for other in {"train", "hotel"} - {domain}:
                probe[other + "_selection"] = None
        checked = check_task(workflow, task["id"], probe)
        if any(
            i["blocking"]
            and (
                i["code"].startswith(domain)
                or i["code"]
                in {"invalid_candidate_reference", "stale_draft", "evidence_needs_revalidation"}
            )
            for i in checked["issues"]
        ):
            domains.add(domain)
    if not task["requires_hotel"]:
        domains.discard("hotel")
    task["revision"] += 1
    task.update(status="pending", issues=[], update_scope=sorted(domains))
    from agents.workflow_state import invalidate_query_binding
    for domain in domains:
        if domain in task.get('query_containers', {}):
            task['query_containers'][domain] = invalidate_query_binding(task['query_containers'][domain], 'repair_requested')
    affected_dates = set()
    if "train" in domains:
        affected_dates.update({"departure_date", "arrival_date"})
    if "schedule" in components:
        affected_dates.update({"check_in", "check_out"})
    for key in affected_dates:
        if task["field_sources"].get(key) in {"proposal", "default", "derived"}:
            task["conditions"].pop(key, None)
            task["field_sources"].pop(key, None)
    if task.get("draft_plan"):
        for domain in domains:
            task["draft_plan"][domain + "_selection"] = None
        if "schedule" in components:
            task["draft_plan"]["schedule"] = {
                k: v
                for k, v in task["draft_plan"].get("schedule", {}).items()
                if k in task["conditions"] and task["field_sources"].get(k) in {"user", "context"}
            }
    _rebind(workflow, task, {"train", "hotel"} - domains)
    if task.get("draft_plan"):
        task["draft_plan"]["task_revision"] = task["revision"]


def apply_repair(workflow, action):
    validate_repair(action, workflow)
    w = deepcopy(workflow)
    fingerprint = _fingerprint(w, action)
    history = w.setdefault("repair_history", [])
    if fingerprint in history:
        raise ValueError("repair_no_progress")
    history.append(fingerprint)
    roots = [t["id"] for t in w["tasks"] if t["id"] in action["task_ids"]]
    boundaries = {
        tid: check_task(w, tid, task_by_id(w, tid).get("draft_plan"))["reconstructed_plan"][
            "schedule"
        ]
        for tid in roots
    }
    for tid in roots:
        _reopen(w, task_by_id(w, tid), action["components"])
    first = min(i for i, t in enumerate(w["tasks"]) if t["id"] in roots)
    dependents = [t["id"] for t in w["tasks"][first + 1 :] if t["id"] not in roots]
    w.update(
        status="running",
        current_task_id=roots[0],
        validation=None,
        stop_reason=None,
        checkpoint={
            "kind": "task_repair",
            "root_task_ids": roots,
            "pending_root_ids": roots[:],
            "pending_dependency_ids": dependents,
            "boundaries_before": boundaries,
            "completed_task_ids": [],
            "resume_task_id": roots[0],
            "affected_task_ids": roots + dependents,
            "expected_workflow_revision": w["revision"],
        },
    )
    return w


def advance_repair(workflow, task_id):
    cp = workflow.get("checkpoint")
    if not cp or cp.get("kind") != "task_repair":
        return
    cp["completed_task_ids"].append(task_id)
    for key in ("pending_root_ids", "pending_dependency_ids"):
        cp[key] = [tid for tid in cp[key] if tid != task_id]
    if cp["pending_root_ids"]:
        workflow["current_task_id"] = cp["resume_task_id"] = cp["pending_root_ids"][0]
        return
    # Walk in route order. Compare to new predecessors, preserving valid later drafts.
    for tid in cp["pending_dependency_ids"][:]:
        task = task_by_id(workflow, tid)
        issues = check_workflow(workflow)["issues"]
        own = check_task(workflow, tid, task.get("draft_plan"))
        incompatible = any(
            i["blocking"]
            and tid == i["task_ids"][-1]
            and i["code"]
            in {
                "departure_before_previous_arrival",
                "departure_before_previous_checkout",
                "route_dependency_mismatch",
            }
            for i in issues
        )
        if incompatible or not own["valid"] or task["status"] == "pending":
            if task["status"] != "pending":
                _reopen(workflow, task, ["train", "schedule"])
            task["needs_dependency_check"] = True
            workflow["current_task_id"] = cp["resume_task_id"] = tid
            return
        cp["pending_dependency_ids"].remove(tid)
    workflow["checkpoint"] = None
    workflow["validation"] = None
