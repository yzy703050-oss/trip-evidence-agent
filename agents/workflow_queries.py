"""Task-owned query evidence; domain names are never the primary key."""
from copy import deepcopy
from datetime import datetime
from agents.workflow_contracts import task_by_id, identifier
from travel_data.candidates import candidate_view
from travel_data.contracts import AgentDataResult, Source
from travel_data.result_guard import guard_domain_result


def record_query(workflow, task_id, parameters, constraints, result, execution, *, domain, refresh=False):
    if domain not in {'train', 'hotel'}:
        raise ValueError('unsupported workflow domain')
    task = task_by_id(workflow, task_id)
    records = workflow['results_by_query']
    previous = next((r for r in records.values() if r['task_id'] == task_id and
                     r['domain'] == domain and r['parameters'] == parameters and r['constraints'] == constraints), None)
    checked = guard_domain_result(domain, {**result, 'query': parameters})
    good = checked.get('status') in {'ok', 'partial'}
    if previous:
        row = deepcopy(previous)
        if row['task_revision'] != task['revision'] or refresh:
            row.setdefault('history', []).append({k: deepcopy(v) for k, v in previous.items() if k != 'history'})
        if refresh and not good and previous.get('items'):
            row.update(status='partial', needs_revalidation=True, refresh_status=checked['status'],
                       refresh_message=checked.get('message'))
        else:
            if refresh and good: row['result_revision'] += 1
            row.update(status=checked['status'], items=deepcopy(checked.get('items', [])),
                       source=checked.get('source'), fetched_at=checked.get('fetched_at'),
                       missing_fields=checked.get('missing_fields', []), message=checked.get('message'),
                       needs_revalidation=False)
    else:
        row = dict(id=identifier('query'), task_id=task_id, task_revision=task['revision'],
                   domain=domain, parameters=deepcopy(parameters), constraints=deepcopy(constraints),
                   result_revision=1, status=checked['status'], items=deepcopy(checked.get('items', [])),
                   source=checked.get('source'), fetched_at=checked.get('fetched_at'),
                   missing_fields=checked.get('missing_fields', []), message=checked.get('message'),
                   needs_revalidation=False, history=[])
    row.update(task_revision=task['revision'], execution=deepcopy(execution))
    records[row['id']] = row
    if row['id'] not in task['query_ids']: task['query_ids'].append(row['id'])
    return row


def as_result(row):
    source = row.get('source')
    source = Source(source['provider'], datetime.fromisoformat(source['fetched_at']), source.get('url')) if source else None
    fetched = datetime.fromisoformat(row['fetched_at']) if row.get('fetched_at') else None
    return AgentDataResult(row['status'], row['parameters'], row['items'], row.get('missing_fields', []),
                           source, fetched, row.get('message'))


def query_views(workflow, task_id, *, limit=5, offset=0):
    if type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
        raise ValueError('invalid candidate window')
    task = task_by_id(workflow, task_id)
    views = []
    for query_id in task['query_ids']:
        row = workflow['results_by_query'][query_id]
        if row['task_revision'] != task['revision']: continue
        view = candidate_view(as_result(row), limit=limit, offset=offset, constraints=row['constraints'],
                              preferences=workflow.get('effective_preferences', {}))
        views.append({**deepcopy(row), 'history': [], 'items': view['items'],
                      'candidate_total': view.get('candidate_total', len(row['items']))})
    return views


def resolve_selection(workflow, task_id, task_revision, selection):
    task = task_by_id(workflow, task_id)
    if task['revision'] != task_revision or not isinstance(selection, dict):
        raise ValueError('stale task selection')
    row = workflow['results_by_query'].get(selection.get('query_id'))
    if not row or row['task_id'] != task_id or row['task_revision'] != task_revision or row['result_revision'] != selection.get('result_revision'):
        raise ValueError('candidate query/version mismatch')
    eligible = candidate_view(as_result(row), limit=max(1, len(row['items'])), offset=0,
                              constraints=row['constraints'], preferences={})['items']
    item = next((i for i in eligible if i['id'] == selection.get('candidate_id')), None)
    if item is None:
        raise ValueError('candidate is absent or fails query constraints')
    return deepcopy(item)
