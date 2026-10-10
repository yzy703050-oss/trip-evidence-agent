"""Program-owned date suggestions and task readiness, without personal-field prompts."""
from datetime import datetime, timedelta, timezone
from copy import deepcopy

from agents.workflow_contracts import effective_conditions, task_by_id

BEIJING = timezone(timedelta(hours=8))


def prepare_task(workflow, task_id, current_time, *, previous_boundary=None):
    task = task_by_id(workflow, task_id)
    effective, sources = effective_conditions(workflow['confirmed_conditions'], task['conditions'], {})
    sources.update(task.get('field_sources', {}))
    index = workflow['tasks'].index(task)
    boundary = previous_boundary or {}
    if not effective.get('departure_date') and not effective.get('arrival_date') and not effective.get('arrival_before'):
        value, source = None, None
        if index == 0 and current_time:
            now = datetime.fromisoformat(current_time).astimezone(BEIJING)
            value, source = (now.date() + timedelta(days=7)).isoformat(), 'default'
        elif boundary.get('arrival_at'):
            value = boundary.get('check_out') or boundary['arrival_at'][:10]
            source = 'derived' if not boundary.get('check_out') else 'proposal'
        if value:
            task['conditions']['departure_date'] = value
            task['field_sources']['departure_date'] = source
            effective['departure_date'], sources['departure_date'] = value, source
    constraints = {k: v for k, v in effective.get('constraints', {}).items() if k != 'total_budget_cny'}
    if boundary.get('arrival_at') and effective.get('departure_date') == boundary['arrival_at'][:10]:
        threshold = boundary['arrival_at'][11:16]
        constraints['departure_time_after'] = max(constraints.get('departure_time_after', '00:00'), threshold)
    missing = [k for k in ('origin', 'destination') if not task.get(k)]
    mode = 'arrival' if effective.get('arrival_date') or effective.get('arrival_before') else 'departure'
    if not effective.get('departure_date') and mode == 'departure': missing.append('departure_date')
    scope = task.get('update_scope', ['train', 'hotel'])
    requests = []
    if not missing and 'train' in scope:
        parameters = dict(origin=task['origin'], destination=task['destination'], passengers=effective['passengers'])
        if mode == 'arrival':
            parameters['arrival_date'] = effective.get('arrival_date') or effective['arrival_before'][:10]
            if effective.get('arrival_before'): parameters['arrival_before'] = effective['arrival_before']
            if effective.get('departure_date'): parameters['departure_date'] = effective['departure_date']
        else:
            parameters['departure_date'] = effective['departure_date']
        if constraints: parameters['constraints'] = constraints
        requests.append(dict(domain='train', parameters=parameters))
    if task['requires_hotel'] and 'hotel' in scope:
        parameters = dict(city=task['destination'], guests=effective['guests'])
        if effective.get('hotel_keywords'): parameters['keywords'] = effective['hotel_keywords']
        requests.append(dict(domain='hotel', parameters=parameters))
    return dict(effective_conditions=deepcopy(effective), field_sources=sources,
                missing_fields=missing, query_requests=requests, query_mode=mode,
                readiness='ready' if not missing else 'partial')
