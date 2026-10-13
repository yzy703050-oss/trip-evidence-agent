"""Evidence-first amendments: selection changes do not invalidate matching queries."""

from copy import deepcopy
from datetime import datetime
from agents.workflow_contracts import task_by_id, validate_conditions, effective_conditions
from agents.workflow_queries import as_result
from agents.workflow_guard import check_workflow, finalize_workflow
from agents.workflow_batches import initialize_query_containers
from agents.workflow_state import invalidate_query_binding
from travel_data.candidates import candidate_view
from travel_data.result_views import build_tool_result_view


def matching_evidence(row, task, effective, context=None):
    if row.get("needs_revalidation") or row["status"] not in {"ok", "partial"}:
        return False
    context = context or {}
    if context.get("current_time") and row["parameters"].get("search_kind") != "hotel_place":
        if not row.get("fetched_at") or (
            datetime.fromisoformat(context["current_time"])
            - datetime.fromisoformat(row["fetched_at"])
        ).total_seconds() > context.get("evidence_max_age_seconds", 3600):
            return False
    params = row["parameters"]
    if row["domain"] == "train":
        keys = {
            "origin": task["origin"],
            "destination": task["destination"],
            "passengers": effective["passengers"],
        }
        keys.update(
            {
                k: effective[k]
                for k in ("departure_date", "arrival_date", "arrival_before")
                if effective.get(k)
            }
        )
    else:
        keys = {"city": task["destination"]}
        if params.get("search_kind") != "hotel_place":
            keys.update(
                {
                    k: effective[k]
                    for k in ("check_in", "check_out", "guests")
                    if effective.get(k) is not None
                }
            )
        elif effective.get("hotel_quote_required"):
            return False
    return all(
        params.get(k, 1 if k in {"passengers", "guests"} else None) == v for k, v in keys.items()
    )


def prepare_saved_trip_amendment(workflow, intent):
    t = task_by_id(workflow, intent["task_id"])
    effective, _ = effective_conditions(workflow["confirmed_conditions"], t["conditions"], {})
    views, covered = [], set()
    for row in workflow["results_by_query"].values():
        if row["task_id"] != t["id"] or not matching_evidence(row, t, effective, intent):
            continue
        constraints = {
            k: v for k, v in effective.get("constraints", {}).items() if k != "total_budget_cny"
        }
        if row["domain"] == "hotel" and effective.get("hotel_keywords"):
            constraints["hotel_keywords"] = effective["hotel_keywords"]
        full = candidate_view(
            as_result(row),
            limit=max(1, len(row["items"])),
            offset=0,
            constraints=constraints,
            preferences=workflow.get("effective_preferences", {}),
        )
        if not effective.get("hotel_keywords") or row["domain"] != "hotel" or full["items"]:
            covered.add(row["domain"])
        views.append(
            build_tool_result_view(
                row["domain"],
                {**row, "items": full["items"], "candidate_total": len(full["items"])},
                limits={"candidate_limit": intent.get("candidate_limit", 5)},
            )
        )
    required = {"train", *({"hotel"} if t["requires_hotel"] else set())}
    return {
        "task": deepcopy(t),
        "candidate_views": views,
        "covered_domains": sorted(covered),
        "missing_domains": sorted(required - covered),
        "policy_context": deepcopy(workflow.get("policy_context")),
    }


def apply_amendment_patch(workflow, patch, context):
    if (
        patch.get("workflow_id") != workflow["id"]
        or patch.get("expected_workflow_revision") != workflow["revision"]
    ):
        raise ValueError("stale workflow patch")
    t = task_by_id(workflow, patch.get("task_id"))
    if patch.get("expected_task_revision") != t["revision"]:
        raise ValueError("stale task patch")
    components = set(patch.get("components", []))
    if not components or not components <= {"train", "hotel", "schedule"}:
        raise ValueError("invalid amendment components")
    affected_domains = components & {"train", "hotel"}
    if "schedule" in components:
        affected_domains |= {"train", *({"hotel"} if t["requires_hotel"] else set())}
    changes = validate_conditions(patch.get("condition_changes", {}))
    hotel_fields = {
        "check_in",
        "check_out",
        "nights",
        "guests",
        "hotel_quote_required",
        "hotel_keywords",
    }
    train_fields = {"departure_date", "arrival_date", "arrival_before", "passengers"}
    if (set(changes) & hotel_fields and not components & {"hotel", "schedule"}) or (
        set(changes) & train_fields and not components & {"train", "schedule"}
    ):
        raise ValueError("condition outside amendment component")
    for field in changes.get("constraints", {}):
        if (
            field.startswith("hotel_")
            and "hotel" not in components
            or field
            in {
                "seat_class",
                "train_max_total_cny",
                "departure_time_after",
                "departure_time_before",
            }
            and "train" not in components
        ):
            raise ValueError("constraint outside amendment component")
    refs = patch.get("selection_refs", {})
    if not isinstance(refs, dict) or set(refs) - components:
        raise ValueError("selection outside amendment scope")
    authorized = context.get("authorized_changes")
    if authorized is not None and any(
        k not in authorized or authorized[k] != v for k, v in changes.items()
    ):
        raise ValueError("unauthorized condition changes")
    candidate = deepcopy(workflow)
    initialize_query_containers(candidate)
    target = task_by_id(candidate, t["id"])
    target["revision"] += 1
    target["conditions"].update({k: deepcopy(v) for k, v in changes.items() if k != "constraints"})
    if "constraints" in changes:
        target["conditions"]["constraints"] = {
            **target["conditions"].get("constraints", {}),
            **changes["constraints"],
        }
    target["field_sources"].update({k: "user" for k in changes})
    target["status"] = "draft"
    target["update_scope"] = sorted(affected_domains)
    target["draft_stage"] = "selection"
    if not target.get("draft_plan"):
        target["draft_plan"] = {
            "task_revision": target["revision"],
            "train_selection": None,
            "hotel_selection": None,
            "schedule": {},
            "unverified_requirements": [],
        }
    target["draft_plan"]["task_revision"] = target["revision"]
    effective, _ = effective_conditions(candidate["confirmed_conditions"], target["conditions"], {})
    coverage = prepare_saved_trip_amendment(candidate, {**context, "task_id": target["id"]})
    for domain, container in target["query_containers"].items():
        matching = [
            r
            for r in candidate["results_by_query"].values()
            if r["task_id"] == target["id"]
            and r["domain"] == domain
            and matching_evidence(
                r, target, effective, context if domain in affected_domains else {}
            )
        ]
        if not matching:
            target["query_containers"][domain] = invalidate_query_binding(
                container, "parameters_changed"
            )
            target["draft_plan"][domain + "_selection"] = None
        else:
            for row in matching:
                row.setdefault("history", []).append(
                    {k: deepcopy(v) for k, v in row.items() if k != "history"}
                )
                row["task_revision"] = target["revision"]
                row["constraints"] = {
                    k: v
                    for k, v in effective.get("constraints", {}).items()
                    if k != "total_budget_cny"
                }
                if domain == "hotel" and effective.get("hotel_keywords"):
                    row["constraints"]["hotel_keywords"] = effective["hotel_keywords"]
            chosen = refs.get(domain) or target["draft_plan"].get(domain + "_selection")
            query_id = chosen.get("query_id") if chosen else matching[-1]["id"]
            container.update(status="ready", current_query_ref=query_id)
    for domain, ref in refs.items():
        target["draft_plan"][domain + "_selection"] = deepcopy(ref)
    for key in ("check_in", "check_out"):
        if key in changes:
            target["draft_plan"]["schedule"][key] = changes[key]
    candidate.update(status="running", validation=None, stop_reason=None)
    if candidate.get("policy_context"):
        from agents.policy_preflight import resolve_policy_for_workflow

        candidate["policy_context"] = resolve_policy_for_workflow(
            candidate["policy_context"], candidate, context
        )
    check = check_workflow(candidate)
    requests = []
    for domain in coverage["missing_domains"]:
        if domain not in affected_domains:
            continue
        params = (
            {
                "origin": target["origin"],
                "destination": target["destination"],
                "departure_date": effective.get("departure_date"),
                "passengers": effective["passengers"],
            }
            if domain == "train"
            else {
                "city": target["destination"],
                "check_in": effective.get("check_in"),
                "check_out": effective.get("check_out"),
                "guests": effective["guests"],
            }
        )
        if domain == "hotel" and effective.get("hotel_keywords"):
            params["keywords"] = effective["hotel_keywords"]
        if context.get("current_time"):
            params["refresh"] = True
        requests.append(
            {
                "task_id": target["id"],
                "task_revision": target["revision"],
                "domain": domain,
                "parameters": params,
            }
        )
    if requests:
        target["status"] = "pending"
    if check["valid"]:
        candidate = finalize_workflow(candidate, check)
    return {
        "candidate_workflow": candidate,
        "validation": check,
        "query_requests": requests,
        "unapplied_patch": None if check["valid"] else deepcopy(patch),
        "gaps": check["issues"],
    }
