"""Model views are separate from persisted business and tool audit objects."""
from copy import deepcopy
import json

AUDIT_FIELDS = {"raw", "raw_response", "raw_payload", "payload", "messages", "tool_calls",
                "tool_results", "candidate_cache", "execution", "history", "audit", "results_by_query"}
RESULT_FIELDS = {"type", "agent", "agent_name", "task_id", "task_revision", "workflow_id",
                 "message_id", "in_reply_to", "status", "summary", "answer", "final_answer",
                 "missing_fields", "issues", "travel_conditions", "completed_conditions", "field_sources",
                 "readiness", "date_options", "preferences", "preference_changes", "changes",
                 "has_preferences", "error", "message", "sources", "needs_requery"}
DOMAIN_FIELDS = {"id", "task_id", "task_revision", "domain", "result_revision", "status",
                 "source", "fetched_at", "missing_fields", "message", "query", "parameters",
                 "constraints", "candidate_total", "view_offset", "needs_revalidation",
                 "refresh_status", "refresh_message"}
FACT_FIELDS = {"id", "kind", "content", "verification", "source", "url", "train_number",
               "origin_station", "destination_station", "departure_time", "arrival_time",
               "departure_at", "arrival_at", "time_evidence", "seat_class", "price_cny", "availability",
               "remaining", "hotel_name", "room_type", "check_in", "check_out", "guests",
               "stay_total_cny", "fees_included", "cancellation", "name", "address", "location",
               "city", "destination", "date", "description", "min_temp_c", "max_temp_c",
               "temperature_c", "weather", "title", "snippet", "text", "provider", "fetched_at",
               "search_kind", "rating", "distance_m", "longitude", "latitude"}


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in AUDIT_FIELDS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return deepcopy(value)


def _fields(value, allowed):
    return {key: _clean(item) for key, item in value.items() if key in allowed} if isinstance(value, dict) else {}


def project_domain_result(value: dict, *, candidate_limit: int = 5) -> dict:
    view = _fields(value, DOMAIN_FIELDS)
    if isinstance(value.get("items"), list):
        view["items"] = [_fields(item, FACT_FIELDS) for item in value["items"][:candidate_limit]]
    return view


def project_child_result(result: dict, *, candidate_limit: int = 5) -> dict:
    view = _fields(result, RESULT_FIELDS)
    if isinstance(result.get("domain_results"), dict):
        view["domain_results"] = {domain: project_domain_result(data, candidate_limit=candidate_limit)
                                  for domain, data in result["domain_results"].items() if isinstance(data, dict)}
    if isinstance(result.get("query_results"), list):
        view["query_results"] = [project_domain_result(row, candidate_limit=candidate_limit)
                                 for row in result["query_results"]]
    return view


def project_workflow_context(workflow: dict, *, task_id: str | None = None,
                             candidate_limit: int = 5) -> dict:
    view = _fields(workflow, {"id", "revision", "status", "original_query", "confirmed_conditions",
        "effective_preferences", "current_task_id", "checkpoint", "stop_reason", "created_at", "updated_at",
        "saved_plan", "validation", "final_answer"})
    task_fields = {"id", "revision", "status", "origin", "destination", "purpose", "purpose_source",
        "requires_hotel", "depends_on", "conditions", "field_sources", "draft_plan", "summary", "issues",
        "query_ids", "update_scope", "rejected_candidates", "preserved_components"}
    view["tasks"] = [_fields(task, task_fields) for task in workflow.get("tasks", [])]
    if task_id is not None:
        from agents.workflow_queries import query_views
        view["relevant_results"] = [project_domain_result(row, candidate_limit=candidate_limit)
                                    for row in query_views(workflow, task_id, limit=candidate_limit)]
    return view


def project_model_context(context: dict) -> dict:
    """Apply the same projection at every main-model entry point."""
    view = _clean(context)
    for key in ("active_workflows", "known_workflows", "candidates"):
        if isinstance(context.get(key), list):
            view[key] = [project_workflow_context(w) for w in context[key]]
    if isinstance(context.get("workflow"), dict):
        view["workflow"] = project_workflow_context(context["workflow"])
    for key in ("latest_child_result",):
        if isinstance(context.get(key), dict):
            view[key] = project_child_result(context[key])
    if isinstance(context.get("domain_results"), dict):
        view["domain_results"] = project_child_result({"domain_results": context["domain_results"]})["domain_results"]
    if isinstance(context.get("relevant_results"), list):
        view["relevant_results"] = [project_domain_result(row) for row in context["relevant_results"]]
    for key in ("results", "previous_results"):
        if isinstance(context.get(key), list):
            rows = []
            for row in context[key]:
                data = row.get("data", row.get("result", {}).get("data", {}))
                rows.append({**_fields(row, {"agent_name", "priority", "status", "answer_role"}),
                             "data": project_child_result(data)})
            view[key] = rows
    return view


def project_session_message(event: dict) -> dict | None:
    if (event.get("scope") != "session" or event.get("type") != "message"
            or event.get("role") not in {"user", "assistant"}):
        return None
    projected = {key: deepcopy(event[key]) for key in
                 ("seq", "turn_id", "scope", "type", "role", "content", "final") if key in event}
    if event.get("role") == "assistant" and event.get("metadata", {}).get("format") == "main_result":
        try:
            result = json.loads(event["content"]) if isinstance(event["content"], str) else event["content"]
            projected["content"] = result.get("final_answer", "")
            facts = {key: result[key] for key in ("travel_conditions", "itinerary", "workflow_id", "workflow_revision") if key in result}
            if facts:
                projected["result_summary"] = _clean(facts)
        except (ValueError, TypeError, AttributeError):
            projected["content"] = "历史答复格式无法读取"
    return projected
