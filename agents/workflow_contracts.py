"""Versioned travel state and decisions. Only the harness changes status or IDs."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from uuid import uuid4

from agents.contracts import validate_constraints

DOMAINS = {'train', 'hotel'}
SOURCES = {'user', 'context', 'preference', 'derived', 'default', 'proposal'}
CONDITION_KEYS = {'start_date', 'end_date', 'departure_date', 'check_in', 'check_out',
                  'passengers', 'guests', 'constraints', 'nights', 'flexible_dates', 'hotel_quote_required'}


def identifier(prefix):
    return f'{prefix}_{uuid4().hex}'


def validate_conditions(value):
    if not isinstance(value, dict) or set(value) - CONDITION_KEYS:
        raise ValueError('unsupported workflow conditions')
    for key, item in value.items():
        if item is None:
            continue
        if key in {'passengers', 'guests', 'nights'}:
            if type(item) is not int or item < 1:
                raise ValueError('invalid person count or nights')
        elif key.endswith('_date') or key in {'check_in', 'check_out'}:
            if not isinstance(item, str):
                raise ValueError('invalid date')
            date.fromisoformat(item)
        elif key in {'flexible_dates', 'hotel_quote_required'}:
            if type(item) is not bool:
                raise ValueError('invalid boolean condition')
        elif key == 'constraints':
            if not isinstance(item, dict):
                raise ValueError('invalid constraints')
            ordinary = {k: v for k, v in item.items() if k != 'total_budget_cny'}
            validate_constraints(ordinary)
            if 'total_budget_cny' in item:
                amount = Decimal(str(item['total_budget_cny']))
                if not amount.is_finite() or amount < 0:
                    raise ValueError('invalid total budget')
    return deepcopy(value)


def effective_conditions(confirmed, task_conditions, preferences):
    validate_conditions(confirmed); validate_conditions(task_conditions)
    effective = {k: deepcopy(v) for k, v in confirmed.items() if v is not None}
    sources = {k: 'context' for k in effective}
    for key, val in task_conditions.items():
        if val is not None:
            if key == 'constraints':
                effective[key] = {**effective.get(key, {}), **val}
            else:
                effective[key] = deepcopy(val)
            sources[key] = 'user'
    effective.setdefault('constraints', {})
    if 'passengers' not in effective:
        effective['passengers'] = 1
        sources['passengers'] = 'default'
    if 'guests' not in effective:
        effective['guests'] = effective['passengers']
        sources['guests'] = 'derived'
    return effective, sources


def create_workflow(proposal, context, *, workflow_id=None):
    if not isinstance(proposal, dict) or not isinstance(proposal.get('tasks'), list) or not proposal['tasks']:
        raise ValueError('workflow needs ordered tasks')
    confirmed = validate_conditions(proposal.get('confirmed_conditions', {}))
    effective, sources = effective_conditions(confirmed, {}, context.get('effective_preferences', {}))
    sources.update({k: 'user' for k, v in confirmed.items() if v is not None})
    tasks = []
    for index, row in enumerate(proposal['tasks']):
        if not isinstance(row, dict) or set(row) - {'origin', 'destination', 'requires_hotel', 'conditions', 'field_sources', 'depends_on'}:
            raise ValueError('invalid task proposal')
        if not all(isinstance(row.get(k), str) and row[k].strip() for k in ('origin', 'destination')):
            raise ValueError('task route needs cities')
        if row['origin'] == row['destination'] or (tasks and row['origin'] != tasks[-1]['destination']):
            raise ValueError('route is not continuous')
        # Optional proposal dependencies refer to prior task indices; IDs are runtime-owned.
        if row.get('depends_on', []) not in ([], [index-1] if index else []):
            raise ValueError('invalid dependency')
        if type(row.get('requires_hotel')) is not bool:
            raise ValueError('task hotel scope is required')
        conditions = validate_conditions(row.get('conditions', {}))
        field_sources = row.get('field_sources', {})
        if not isinstance(field_sources, dict) or any(v not in SOURCES for v in field_sources.values()):
            raise ValueError('invalid field source')
        tasks.append(dict(id=identifier('task'), revision=1, status='pending',
                          origin=row['origin'], destination=row['destination'],
                          requires_hotel=row['requires_hotel'], depends_on=[tasks[-1]['id']] if tasks else [],
                          conditions=conditions, field_sources={k: field_sources.get(k, 'proposal') for k in conditions},
                          draft_plan=None, summary='', query_ids=[], issues=[]))
    if len(tasks) > 1 and tasks[-1]['destination'] == tasks[0]['origin'] and tasks[-1]['requires_hotel']:
        raise ValueError('return task must not require a destination hotel')
    return dict(id=workflow_id or identifier('workflow'), revision=1, status='running',
                original_query=context.get('original_query', ''), confirmed_conditions=confirmed,
                effective_conditions=effective, field_sources=sources, current_task_id=tasks[0]['id'],
                tasks=tasks, results_by_query={}, candidate_cache={}, checkpoint=None, stop_reason=None,
                validation=None, audit={'model_calls': 0, 'tool_calls': 0, 'info_executions': 0})


def task_by_id(workflow, task_id):
    task = next((t for t in workflow['tasks'] if t['id'] == task_id), None)
    if task is None:
        raise ValueError('unknown task')
    return task


def validate_action(action, workflow):
    if not isinstance(action, dict):
        raise ValueError('action must be an object')
    value = deepcopy(action)
    name = value.get('action')
    allowed = {
        'dispatch': {'action', 'task_id', 'task_revision', 'goal', 'query_requests'},
        'draft_task': {'action', 'task_id', 'task_revision', 'draft_plan', 'summary'},
        'ask_user': {'action', 'question', 'reason', 'affected_task_ids', 'suggested_changes', 'resume_task_id'},
        'validate_workflow': {'action', 'workflow_revision', 'analysis'},
        'finish': {'action', 'status', 'final_answer', 'gaps'},
    }
    if name not in allowed or set(value) - allowed[name]:
        raise ValueError('unsupported workflow action or fields')
    if name in {'dispatch', 'draft_task'}:
        task = task_by_id(workflow, value.get('task_id'))
        if value.get('task_revision', task['revision']) != task['revision']:
            raise ValueError('stale task action')
        if task['id'] != workflow['current_task_id']:
            raise ValueError('only current task can be modified')
        value['task_revision'] = task['revision']
        if name == 'dispatch':
            if not isinstance(value.get('goal'), str) or not value['goal'].strip():
                raise ValueError('dispatch needs a goal')
            requests = value.get('query_requests')
            if not isinstance(requests, list):
                raise ValueError('dispatch needs query requests')
            for query in requests:
                if not isinstance(query, dict) or query.get('domain') not in DOMAINS or not isinstance(query.get('parameters'), dict):
                    raise ValueError('query outside train/hotel scope')
                if query['domain'] == 'hotel' and not task['requires_hotel']:
                    raise ValueError('no hotel in return task')
        elif not isinstance(value.get('draft_plan'), dict) or not isinstance(value.get('summary', ''), str):
            raise ValueError('draft needs a plan')
    elif name == 'ask_user':
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ('question', 'reason')):
            raise ValueError('ask_user needs question and reason')
        ids = value.get('affected_task_ids')
        if not isinstance(ids, list) or not ids:
            raise ValueError('ask_user needs affected tasks')
        for task_id in ids: task_by_id(workflow, task_id)
        if not isinstance(value.get('suggested_changes', []), list):
            raise ValueError('invalid suggestions')
        if value.get('resume_task_id') is not None: task_by_id(workflow, value['resume_task_id'])
    elif name == 'validate_workflow':
        if value.get('workflow_revision') != workflow['revision']:
            raise ValueError('stale workflow action')
    elif name == 'finish':
        if value.get('status') not in {'completed', 'partial'} or not isinstance(value.get('final_answer'), str):
            raise ValueError('invalid finish')
        if not isinstance(value.get('gaps', []), list):
            raise ValueError('invalid finish gaps')
    return value


def apply_user_update(workflow, updates):
    if not isinstance(updates, dict) or set(updates) - {'confirmed_conditions', 'task_updates', 'resume_task_id'}:
        raise ValueError('invalid user update')
    w = deepcopy(workflow)
    changed = []
    global_updates = validate_conditions(updates.get('confirmed_conditions', {}))
    if any(w['confirmed_conditions'].get(k) != v for k, v in global_updates.items()):
        w['confirmed_conditions'].update(global_updates)
        changed.append(0)
    for update in updates.get('task_updates', []):
        if not isinstance(update, dict) or set(update) - {'task_id', 'conditions'}:
            raise ValueError('invalid task update')
        task = task_by_id(w, update.get('task_id'))
        conditions = validate_conditions(update.get('conditions', {}))
        if any(task['conditions'].get(k) != v for k, v in conditions.items()):
            task['conditions'].update(conditions)
            task['field_sources'].update({k: 'user' for k in conditions})
            changed.append(w['tasks'].index(task))
    w['effective_conditions'], w['field_sources'] = effective_conditions(w['confirmed_conditions'], {}, {})
    w['field_sources'].update({k: 'user' for k, v in w['confirmed_conditions'].items() if v is not None})
    if changed:
        start = min(changed)
        for task in w['tasks'][start:]:
            task['revision'] += 1
            task['status'] = 'pending'
            task['issues'] = []
        w['current_task_id'] = w['tasks'][start]['id']
    else:
        resume = updates.get('resume_task_id') or (w.get('checkpoint') or {}).get('resume_task_id') or w['current_task_id']
        task = task_by_id(w, resume)
        if task['status'] == 'needs_input': task['status'] = 'pending'
        w['current_task_id'] = resume
    w.update(status='running', checkpoint=None, stop_reason=None, validation=None)
    return w
