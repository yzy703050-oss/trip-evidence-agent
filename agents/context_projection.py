"""Model views are separate from persisted business and tool audit objects."""

from copy import deepcopy
import json

AUDIT_FIELDS = {
    "raw",
    "raw_response",
    "raw_payload",
    "payload",
    "messages",
    "tool_calls",
    "tool_results",
    "candidate_cache",
    "execution",
    "history",
    "audit",
    "results_by_query",
}
RESULT_FIELDS = {
    "type",
    "agent",
    "agent_name",
    "task_id",
    "task_revision",
    "workflow_id",
    "message_id",
    "in_reply_to",
    "status",
    "summary",
    "answer",
    "final_answer",
    "missing_fields",
    "issues",
    "travel_conditions",
    "completed_conditions",
    "field_sources",
    "readiness",
    "date_options",
    "preferences",
    "preference_changes",
    "changes",
    "has_preferences",
    "error",
    "message",
    "sources",
    "needs_requery",
}
DOMAIN_FIELDS = {
    "id",
    "task_id",
    "task_revision",
    "domain",
    "result_revision",
    "status",
    "source",
    "fetched_at",
    "missing_fields",
    "message",
    "query",
    "parameters",
    "constraints",
    "candidate_total",
    "view_offset",
    "needs_revalidation",
    "refresh_status",
    "refresh_message",
}
FACT_FIELDS = {
    "id",
    "kind",
    "content",
    "verification",
    "source",
    "url",
    "train_number",
    "origin_station",
    "destination_station",
    "departure_time",
    "arrival_time",
    "departure_at",
    "arrival_at",
    "time_evidence",
    "seat_class",
    "price_cny",
    "availability",
    "remaining",
    "hotel_name",
    "room_type",
    "check_in",
    "check_out",
    "guests",
    "stay_total_cny",
    "fees_included",
    "cancellation",
    "name",
    "address",
    "location",
    "city",
    "destination",
    "date",
    "description",
    "min_temp_c",
    "max_temp_c",
    "temperature_c",
    "weather",
    "title",
    "snippet",
    "text",
    "provider",
    "fetched_at",
    "search_kind",
    "rating",
    "distance_m",
    "longitude",
    "latitude",
}


def _clean(value):
    if isinstance(value, dict):
        return {key: (_source_view(item) if key == 'source' else _clean(item))
                for key, item in value.items() if key not in AUDIT_FIELDS | {'url', 'time_evidence'}}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return deepcopy(value)


def _fields(value, allowed):
    return (
        {key: (_source_view(item) if key == 'source' else _clean(item)) for key, item in value.items() if key in allowed}
        if isinstance(value, dict)
        else {}
    )


def _source_view(value):
    if isinstance(value, dict):
        return {key: value[key] for key in ('provider',) if key in value}
    return deepcopy(value)


def decision_requirements(context):
    text = str(context.get('original_query', '')) + str(context.get('current_user_query', ''))
    text += json.dumps(context.get('effective_preferences', {}), ensure_ascii=False)
    return {field for field, words in {
        'rating': ('评分', 'rating'), 'distance_m': ('距离', '附近', '步行', '靠近', 'distance'),
        'cancellation': ('取消', '退订', '退款', 'cancellation'),
    }.items() if any(word in text for word in words)}


def project_domain_result(value: dict, *, candidate_limit: int = 5, requirements=None) -> dict:
    view = _fields(value, DOMAIN_FIELDS - {'query', 'parameters', 'constraints', 'fetched_at'})
    required = set(requirements or ())
    if isinstance(value.get("items"), list):
        items = []
        for item in value['items'][:candidate_limit]:
            domain = value.get('domain') or ('train' if 'train_number' in item else 'hotel' if 'hotel_name' in item else None)
            if domain == 'train':
                allowed = {'id', 'train_number', 'origin_station', 'destination_station', 'departure_at',
                           'arrival_at', 'departure_time', 'arrival_time', 'seat_class', 'price_cny', 'availability', 'remaining'}
            elif domain == 'hotel':
                allowed = {'id', 'kind', 'hotel_name', 'address', 'city', 'availability'}
                if item.get('kind') != 'hotel_place':
                    allowed |= {'room_type', 'stay_total_cny', 'fees_included'}
                allowed |= required & {'rating', 'distance_m', 'cancellation'}
            else:
                allowed = FACT_FIELDS
            facts = _fields(item, allowed)
            for key in ('departure', 'arrival'):
                if facts.get(key + '_at'):
                    facts.pop(key + '_time', None)
            source = _source_view(item.get('source'))
            if source and source != view.get('source'):
                facts['source'] = source
            items.append(facts)
        view['items'] = items
    return view


def project_child_result(result: dict, *, candidate_limit: int = 5, requirements=None, include_candidates=True) -> dict:
    view = _fields(result, RESULT_FIELDS)
    if isinstance(result.get("domain_results"), dict):
        view["domain_results"] = {
            domain: project_domain_result({**data, 'domain': domain}, candidate_limit=candidate_limit if include_candidates else 0, requirements=requirements)
            for domain, data in result["domain_results"].items()
            if isinstance(data, dict)
        }
    if isinstance(result.get("query_results"), list):
        view["query_results"] = [
            project_domain_result(row, candidate_limit=candidate_limit if include_candidates else 0, requirements=requirements)
            for row in result["query_results"]
        ]
    if not include_candidates:
        view.pop('domain_results', None)
        view.pop('date_options', None)
        for row in view.get('query_results', []):
            row.pop('items', None)
    return view


def project_workflow_context(
    workflow: dict, *, task_id: str | None = None, candidate_limit: int = 5
) -> dict:
    view = _fields(
        workflow,
        {
            "id",
            "revision",
            "status",
            "original_query",
            "summary",
            "task_count",
            "confirmed_conditions",
            "effective_preferences",
            "current_task_id",
            "checkpoint",
            "stop_reason",
            "created_at",
            "updated_at",
            "validation",
        },
    )
    task_fields = {
        "id",
        "revision",
        "status",
        "origin",
        "destination",
        "purpose",
        "purpose_source",
        "requires_hotel",
        "depends_on",
        "conditions",
        "field_sources",
        "summary",
        "issues",
    }
    view["tasks"] = [_fields(task, task_fields) for task in workflow.get("tasks", [])]
    for task in view['tasks']:
        task['conditions'] = {k: v for k, v in task.get('conditions', {}).items()
                              if k in {'departure_date', 'arrival_date', 'arrival_before', 'check_in', 'check_out',
                                       'nights', 'flexible_dates', 'passengers', 'guests', 'constraints',
                                       'hotel_quote_required', 'hotel_keywords'}}
        if 'summary' in task:
            task['summary'] = str(task['summary'])[:160]
    if view.get('validation'):
        view['validation'] = project_validation(view['validation'])
    if task_id is not None:
        from agents.workflow_queries import query_views

        view["relevant_results"] = [
            project_domain_result(row, candidate_limit=candidate_limit)
            for row in query_views(workflow, task_id, limit=candidate_limit)
        ]
    return view


def project_model_context(context: dict) -> dict:
    """Apply the same projection at every main-model entry point."""
    view = _clean(context)
    requirements = decision_requirements(context)
    for key in ("active_workflows", "known_workflows", "candidates"):
        if isinstance(context.get(key), list):
            view[key] = [project_workflow_context(w) for w in context[key]]
    if isinstance(context.get("workflow"), dict):
        view["workflow"] = project_workflow_context(context["workflow"])
        view.pop('workflow_overview', None)
    if isinstance(context.get('current_task'), dict):
        view['current_task'] = _fields(context['current_task'], {'id', 'revision', 'status', 'origin', 'destination',
            'purpose', 'purpose_source', 'requires_hotel', 'depends_on', 'conditions', 'field_sources',
            'summary', 'issues', 'draft_plan', 'update_scope', 'needs_dependency_check', 'rejected_candidates'})
    if isinstance(context.get('preflight'), dict):
        view['preflight'] = _fields(context['preflight'], {'readiness', 'missing_fields', 'query_requests', 'query_mode'})
    for key in ("latest_child_result",):
        if isinstance(context.get(key), dict):
            view[key] = project_child_result(context[key], requirements=requirements,
                include_candidates=not bool(context.get('relevant_results')))
    if isinstance(context.get("domain_results"), dict):
        view["domain_results"] = project_child_result(
            {"domain_results": context["domain_results"]}, requirements=requirements
        )["domain_results"]
    if isinstance(context.get("relevant_results"), list):
        view["relevant_results"] = [
            project_domain_result(row, requirements=requirements) for row in context["relevant_results"]
        ]
    for key in ("results", "previous_results"):
        if isinstance(context.get(key), list):
            rows = []
            for row in context[key]:
                data = row.get("data", row.get("result", {}).get("data", {}))
                rows.append(
                    {
                        **_fields(row, {"agent_name", "priority", "status", "answer_role"}),
                        "data": project_child_result(data, requirements=requirements,
                            include_candidates=not bool(context.get('relevant_results') or context.get('domain_results'))),
                    }
                )
            view[key] = rows
    if isinstance(view.get('action_feedback'), dict):
        view['action_feedback'] = project_validation(view['action_feedback'])
    return view


def project_validation(value):
    return _fields(value, {'valid', 'issues', 'validated_revisions', 'signature', 'code', 'message', 'allowed_actions', 'query_requests'})


def project_delegation_context(context: dict) -> dict:
    """A delegation receives task facts, not the main conversation snapshot."""
    if context.get('current_task'):
        view = _clean({key: value for key, value in context.items() if key in {
            'original_query', 'current_user_query', 'current_time', 'current_task', 'effective_conditions',
            'confirmed_conditions', 'effective_preferences', 'field_sources', 'previous_boundary', 'preflight', 'feedback'}})
        view['current_task'] = _fields(context['current_task'], {'id', 'revision', 'origin', 'destination',
            'purpose', 'requires_hotel', 'conditions', 'field_sources', 'update_scope'})
        view['recent_messages'] = []
        return view
    view = _clean(
        {
            key: value
            for key, value in context.items()
            if key not in {"recent_messages", "history_summary"}
        }
    )
    # Keep the public context shape while sending no main transcript.
    view["recent_messages"] = []
    return view


def project_session_result(result: dict) -> dict:
    """Persist reply and facts while retaining the caller's complete result."""
    return {key: _clean(deepcopy(result[key])) for key in (
        'final_answer', 'status', 'finalization_method', 'workflow_id',
        'workflow_revision', 'travel_conditions', 'itinerary',
        'error_code', 'error', 'stop_reason',
    ) if key in result}


def project_session_message(event: dict) -> dict | None:
    if (
        event.get("scope") != "session"
        or event.get("type") != "message"
        or event.get("role") not in {"user", "assistant"}
    ):
        return None
    projected = {
        key: deepcopy(event[key])
        for key in ("seq", "turn_id", "scope", "type", "role", "content", "final", "result_summary", "timestamp")
        if key in event
    }
    if (
        event.get("role") == "assistant"
        and event.get("metadata", {}).get("format") == "main_result"
    ):
        try:
            result = (
                json.loads(event["content"])
                if isinstance(event["content"], str)
                else event["content"]
            )
            projected["content"] = result.get("final_answer", "")
            facts = {
                key: result[key]
                for key in ("travel_conditions", "itinerary", "workflow_id", "workflow_revision")
                if key in result
            }
            if facts:
                projected["result_summary"] = _clean(facts)
        except (ValueError, TypeError, AttributeError):
            projected["content"] = "历史答复格式无法读取"
    return projected
