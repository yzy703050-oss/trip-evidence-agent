"""Versioned travel state and decisions. Only the harness changes status or IDs."""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from agents.contracts import validate_constraints

DOMAINS = {'train', 'hotel'}
SOURCES = {'user', 'context', 'preference', 'derived', 'default', 'proposal'}
CONDITION_KEYS = {'start_date', 'end_date', 'departure_date', 'check_in', 'check_out',
                  'arrival_date', 'arrival_before', 'hotel_keywords',
                  'passengers', 'guests', 'constraints', 'nights', 'flexible_dates', 'hotel_quote_required'}
PURPOSES = {'visit', 'business', 'return', 'transit', 'unspecified'}


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
        elif key == 'arrival_before':
            stamp = datetime.fromisoformat(item)
            if stamp.tzinfo is None: raise ValueError('arrival deadline needs timezone')
        elif key == 'hotel_keywords':
            if not isinstance(item, str) or not item.strip(): raise ValueError('invalid hotel keywords')
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
    confirmed = deepcopy(proposal.get('confirmed_conditions', {}))
    if not isinstance(confirmed, dict): raise ValueError('invalid confirmed conditions')
    rows = proposal['tasks']
    if any(not isinstance(row, dict) for row in rows): raise ValueError('task proposal must be an object')
    # These model metadata fields duplicate the route/scope, not query constraints.
    # Check consistency before projecting onto the canonical condition contract.
    route = [r.get('destination') for r in rows if isinstance(r, dict)]
    if 'origin' in confirmed and confirmed.pop('origin') != rows[0].get('origin'):
        raise ValueError('route metadata origin mismatch')
    if 'destination' in confirmed:
        destination=confirmed.pop('destination')
        if len(rows)!=1 or destination!=rows[0]['destination']: raise ValueError('route metadata destination mismatch')
    if 'requires_hotel' in confirmed:
        hotel=confirmed.pop('requires_hotel')
        if type(hotel) is not bool or hotel!=any(r.get('requires_hotel') for r in rows): raise ValueError('hotel scope metadata mismatch')
    if 'scope' in confirmed and confirmed.pop('scope') not in {'交通与住宿','火车和酒店','train_hotel'}:
        raise ValueError('unsupported scope metadata')
    if 'return_to' in confirmed and confirmed.pop('return_to') != rows[-1].get('destination'):
        raise ValueError('route metadata return mismatch')
    if 'destinations' in confirmed:
        destinations = confirmed.pop('destinations')
        if destinations not in (route, route[:-1] if route and route[-1] == rows[0].get('origin') else route):
            raise ValueError('route metadata destinations mismatch')
    constraints = confirmed.get('constraints', {})
    if isinstance(constraints, dict):
        for key in ('only_train_hotel', 'no_weather', 'no_guide'):
            if key in constraints and constraints.pop(key) is not True:
                raise ValueError('workflow scope metadata mismatch')
    confirmed = validate_conditions(confirmed)
    effective, sources = effective_conditions(confirmed, {}, context.get('effective_preferences', {}))
    sources.update({k: 'user' for k, v in confirmed.items() if v is not None})
    tasks = []
    for index, row in enumerate(proposal['tasks']):
        if not isinstance(row, dict) or set(row) - {'origin', 'destination', 'requires_hotel', 'purpose', 'purpose_source', 'conditions', 'field_sources', 'depends_on'}:
            raise ValueError('invalid task proposal')
        if not isinstance(row.get('destination'), str) or not row['destination'].strip() or (
                row.get('origin') is not None and (not isinstance(row['origin'], str) or not row['origin'].strip())):
            raise ValueError('task route needs cities')
        if row.get('origin') == row['destination'] or (tasks and row.get('origin') is not None and row['origin'] != tasks[-1]['destination']):
            raise ValueError('route is not continuous')
        if tasks and row.get('origin') is None: row = {**row, 'origin': tasks[-1]['destination']}
        purpose = row.get('purpose', 'unspecified')
        if purpose=='unspecified' and row.get('purpose_source')=='unspecified': row={**row,'purpose_source':'proposal'}
        if purpose not in PURPOSES or row.get('purpose_source', 'proposal') not in SOURCES:
            raise ValueError('invalid task purpose')
        # Optional proposal dependencies refer to prior task indices; IDs are runtime-owned.
        if row.get('depends_on', []) not in ([], [index-1] if index else []):
            raise ValueError('invalid dependency')
        if type(row.get('requires_hotel')) is not bool:
            raise ValueError('task hotel scope is required')
        conditions = validate_conditions(row.get('conditions', {}))
        field_sources = row.get('field_sources', {})
        if not isinstance(field_sources,dict): raise ValueError('invalid field source')
        field_sources = deepcopy(field_sources)
        for key,source in list(field_sources.items()):
            if key.startswith('conditions.'):
                ordinary=key.removeprefix('conditions.')
                if ordinary in field_sources and field_sources[ordinary]!=source: raise ValueError('conflicting field sources')
                field_sources[ordinary]=field_sources.pop(key)
        for key,source in list(field_sources.items()):
            if source=='missing' and row.get(key,conditions.get(key)) is None: field_sources.pop(key)
        if any(v not in SOURCES for v in field_sources.values()):
            raise ValueError('invalid field source')
        if index == 0 and not conditions.get('departure_date') and confirmed.get('start_date'):
            conditions['departure_date'] = confirmed['start_date']
            field_sources['departure_date'] = 'derived'
        tasks.append(dict(id=identifier('task'), revision=1, status='pending',
                          origin=row.get('origin'), destination=row['destination'],
                          purpose=purpose, purpose_source=row.get('purpose_source', 'proposal'),
                          requires_hotel=row['requires_hotel'], depends_on=[tasks[-1]['id']] if tasks else [],
                          conditions=conditions, field_sources={k: field_sources.get(k, 'proposal') for k in conditions},
                          draft_plan=None, summary='', query_ids=[], issues=[]))
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
        'dispatch': {'action', 'task_id', 'task_revision', 'goal', 'query_requests', 'mode'},
        'draft_task': {'action', 'task_id', 'task_revision', 'draft_plan', 'summary'},
        'ask_user': {'action', 'question', 'reason', 'affected_task_ids', 'suggested_changes', 'resume_task_id'},
        'validate_workflow': {'action', 'workflow_revision', 'analysis'},
        'finish': {'action', 'status', 'final_answer', 'gaps'},
    }
    if name not in allowed:
        raise ValueError('unsupported workflow action')
    if set(value) - allowed[name]:
        raise ValueError(f"unsupported fields: {sorted(set(value)-allowed[name])}; allowed fields: {sorted(allowed[name])}")
    if name in {'dispatch', 'draft_task'}:
        task = task_by_id(workflow, value.get('task_id'))
        if value.get('task_revision', task['revision']) != task['revision']:
            raise ValueError('stale task action')
        if task['id'] != workflow['current_task_id']:
            raise ValueError('only current task can be modified')
        value['task_revision'] = task['revision']
        if name == 'dispatch':
            if value.get('mode', 'query_candidates') not in {'complete_conditions', 'query_candidates'}:
                raise ValueError('invalid dispatch mode')
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
        if not isinstance(update, dict) or set(update) - {'task_id', 'conditions', 'origin', 'destination', 'requires_hotel'}:
            raise ValueError('invalid task update')
        task = task_by_id(w, update.get('task_id'))
        index = w['tasks'].index(task)
        for key in ('origin', 'destination', 'requires_hotel'):
            if key not in update or task[key] == update[key]: continue
            value = update[key]
            if key == 'requires_hotel':
                if type(value) is not bool: raise ValueError('invalid hotel scope')
            elif not isinstance(value, str) or not value.strip():
                raise ValueError('invalid route update')
            previous = task[key]; task[key] = value; changed.append(index)
            if key == 'destination' and index+1 < len(w['tasks']) and w['tasks'][index+1]['origin'] == previous:
                w['tasks'][index+1]['origin'] = value
        conditions = validate_conditions(update.get('conditions', {}))
        if index == 0 and conditions.get('departure_date'):
            if global_updates.get('start_date') and global_updates['start_date'] != conditions['departure_date']:
                raise ValueError('first departure contradicts explicit overall start')
            w['confirmed_conditions']['start_date'] = conditions['departure_date']
        if any(task['conditions'].get(k) != v for k, v in conditions.items()):
            task['conditions'].update(conditions)
            task['field_sources'].update({k: 'user' for k in conditions})
            changed.append(w['tasks'].index(task))
    if any(t['origin'] == t['destination'] or (i and t['origin'] != w['tasks'][i-1]['destination']) for i, t in enumerate(w['tasks'])):
        raise ValueError('user route updates are inconsistent')
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
