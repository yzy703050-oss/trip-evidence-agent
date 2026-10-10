"""Minimal, versioned feedback updates; unrelated choices remain intact."""
from copy import deepcopy

from agents.workflow_contracts import create_workflow, task_by_id, validate_conditions


def _rebind(workflow, task, domains):
    for query_id in task.get('query_ids', []):
        row = workflow['results_by_query'][query_id]
        if row['domain'] in domains:
            row['task_revision'] = task['revision']
    if task.get('draft_plan'):
        task['draft_plan']['task_revision'] = task['revision']


def apply_travel_update(workflow, update, *, context=None):
    w = deepcopy(workflow)
    target = update.get('target', {})
    if target.get('workflow_id') != w['id']: raise ValueError('wrong travel identity')
    kind = update.get('update_type')
    if kind not in {'supplement', 'change', 'regenerate', 'replace', 'change_route', 'adopt', 'pause', 'cancel'}:
        raise ValueError('invalid travel update type')
    components = set(target.get('components', []))
    if not components <= {'train', 'hotel', 'route', 'schedule'}: raise ValueError('invalid component scope')
    w['last_update'] = deepcopy(update)
    if kind in {'pause', 'cancel'}:
        w.update(status='paused' if kind == 'pause' else 'cancelled', stop_reason=kind)
        return w
    if kind == 'change_route':
        route_context = deepcopy(context) if context is not None else {}
        if context is not None:
            route_context['original_query'] = w.get('original_query', '') + '\n' + context.get('original_query', '')
        replacement = create_workflow({'confirmed_conditions': w['confirmed_conditions'],
                                       'tasks': update.get('route_tasks')}, route_context,
                                      workflow_id=w['id'], require_stay_proposals=context is not None)
        old = {(t['origin'], t['destination']): t for t in w['tasks']}
        rows = []
        for candidate in replacement['tasks']:
            previous = old.pop((candidate['origin'], candidate['destination']), None)
            task = deepcopy(previous) if previous else candidate
            dependency = [rows[-1]['id']] if rows else []
            if previous and task['depends_on'] != dependency:
                task['revision'] += 1
                task['status'] = 'pending'
                task['needs_dependency_check'] = True
                _rebind(w, task, {'train', 'hotel'})
            task['depends_on'] = dependency
            rows.append(task)
        w['tasks'] = rows
        w['current_task_id'] = next((t['id'] for t in rows if t['status'] == 'pending'), rows[0]['id'])
        w.update(status='running', checkpoint=None, validation=None, stop_reason=None)
        return w
    ids = target.get('task_ids', [])
    if not isinstance(ids, list) or not ids: raise ValueError('travel update needs task identity')
    selected = [task_by_id(w, task_id) for task_id in ids]
    if kind == 'adopt':
        from agents.workflow_queries import resolve_selection
        if update.get('condition_updates') or update.get('rejected_candidate_ids'):
            raise ValueError('adoption cannot change conditions')
        selections=update.get('selections',{})
        if not isinstance(selections,dict) or set(selections)-set(ids): raise ValueError('invalid adoption scope')
        for task in selected:
            chosen=selections.get(task['id'],{})
            if not isinstance(chosen,dict) or set(chosen)-{'train','hotel'} or (components and set(chosen)-components):
                raise ValueError('invalid adoption components')
            for domain,ref in chosen.items():
                row=w['results_by_query'].get(ref.get('query_id')) if isinstance(ref,dict) else None
                if not row or row['domain']!=domain: raise ValueError('invalid adoption evidence')
                resolve_selection(w,task['id'],task['revision'],ref)
            if chosen and task.get('draft_plan') is None: raise ValueError('adoption needs a draft')
            task['revision']+=1
            _rebind(w,task,{'train','hotel'})
            for domain,ref in chosen.items(): task['draft_plan'][domain+'_selection']=deepcopy(ref)
            task['status']='draft' if task.get('draft_plan') else 'pending'
            task['user_adopted']=True
        w.update(status='running',checkpoint=None,validation=None,stop_reason=None,current_task_id=selected[0]['id'])
        return w
    changes = deepcopy(update.get('condition_updates', {}))
    if not isinstance(changes, dict): raise ValueError('invalid condition updates')
    if 'hotel_brands' in changes:
        brands = changes.pop('hotel_brands')
        if not isinstance(brands, list) or not brands or not all(isinstance(x, str) and x.strip() for x in brands):
            raise ValueError('invalid hotel brands')
        changes['hotel_keywords'] = ' '.join(brands)
    route_changes = {k: changes.pop(k) for k in ('origin', 'destination', 'requires_hotel', 'purpose') if k in changes}
    conditions = validate_conditions(changes)
    changed_domains = set(components) & {'train', 'hotel'}
    timing = bool({'departure_date', 'arrival_date', 'arrival_before', 'check_in', 'check_out', 'nights', 'start_date', 'end_date'} & set(conditions))
    if 'origin' in route_changes: changed_domains.add('train')
    if 'requires_hotel' in route_changes: changed_domains.add('hotel')
    if 'destination' in route_changes or timing or {'passengers', 'guests'} & set(conditions) or components & {'route', 'schedule'}:
        changed_domains = {'train', 'hotel'}
    if not changed_domains: changed_domains = {'train', 'hotel'}
    first = min(w['tasks'].index(t) for t in selected)
    for task in selected:
        task['revision'] += 1
        task['status'] = 'pending'
        task['issues'] = []
        if (conditions.get('arrival_date') or conditions.get('arrival_before')) and not conditions.get('departure_date') and task.get('field_sources',{}).get('departure_date') in {'default','proposal','derived'}:
            task['conditions'].pop('departure_date',None)
            task['field_sources'].pop('departure_date',None)
        merged=deepcopy(conditions)
        if 'constraints' in merged:
            merged['constraints']={**task['conditions'].get('constraints',{}),**merged['constraints']}
        task['conditions'].update(merged)
        task['field_sources'].update({k: 'user' for k in conditions})
        for key, value in route_changes.items():
            if key in {'origin', 'destination'} and (not isinstance(value, str) or not value.strip()):
                raise ValueError('invalid route update')
            if key == 'requires_hotel' and type(value) is not bool: raise ValueError('invalid hotel scope')
            if key == 'purpose':
                from agents.workflow_contracts import PURPOSES
                if value not in PURPOSES: raise ValueError('invalid purpose')
            old_value = task.get(key)
            task[key] = value
            index = w['tasks'].index(task)
            if key == 'destination' and index+1 < len(w['tasks']) and w['tasks'][index+1]['origin'] == old_value:
                w['tasks'][index+1]['origin'] = value
        if task is w['tasks'][0] and conditions.get('departure_date'):
            w['confirmed_conditions']['start_date'] = conditions['departure_date']
        task['update_scope'] = sorted(changed_domains)
        if (w.get('checkpoint') or {}).get('kind') == 'required_conditions' and task['requires_hotel'] and not (task.get('draft_plan') or {}).get('hotel_selection'):
            # Resuming a pre-query checkpoint must still build the original hotel plan.
            task['update_scope'] = sorted(changed_domains | {'hotel'})
        rejected = update.get('rejected_candidate_ids', [])
        if not isinstance(rejected, list) or not all(isinstance(x, str) for x in rejected): raise ValueError('invalid rejection')
        for domain in changed_domains:
            task.setdefault('rejected_candidates', {}).setdefault(domain, [])
            if len(changed_domains) == 1:
                task['rejected_candidates'][domain] = list(dict.fromkeys(task['rejected_candidates'][domain] + rejected))
            if task.get('draft_plan'): task['draft_plan'][domain+'_selection'] = None
        _rebind(w, task, {'train', 'hotel'}-changed_domains)
    if timing or route_changes or 'train' in changed_domains:
        for task in w['tasks'][first+1:]:
            if task in selected: continue
            task['revision'] += 1
            task['status'] = 'pending'
            task['needs_dependency_check'] = True
            _rebind(w, task, {'train', 'hotel'})
    for index, task in enumerate(w['tasks']):
        if task['origin'] == task['destination'] or (index and task['origin'] != w['tasks'][index-1]['destination']):
            raise ValueError('route is not continuous')
    w['current_task_id'] = w['tasks'][first]['id']
    w.update(status='running', checkpoint=None, validation=None, stop_reason=None)
    return w
