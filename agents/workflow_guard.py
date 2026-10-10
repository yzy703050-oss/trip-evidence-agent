"""Deterministic route feasibility and sourced display reconstruction."""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json

from agents.workflow_contracts import task_by_id, effective_conditions
from agents.workflow_queries import resolve_selection
from travel_data.plan_guard import _offer
from travel_data.budget import build_budget


def state_signature(workflow):
    payload = {'conditions': workflow['confirmed_conditions'],
               'tasks': [{k: t[k] for k in ('id', 'revision', 'origin', 'destination', 'requires_hotel', 'conditions', 'draft_plan')}
                         for t in workflow['tasks']], 'queries': workflow['results_by_query']}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def issue(code, ids, message, *, blocking=True, evidence=None):
    return dict(code=code, task_ids=ids, blocking=blocking, message=message, evidence=evidence)


def check_task(workflow, task_id, draft):
    task = task_by_id(workflow, task_id)
    issues = []
    def add(code, message, **kwargs): issues.append(issue(code, [task_id], message, **kwargs))
    effective, _ = effective_conditions(workflow['confirmed_conditions'], task['conditions'], {})
    constraints = effective.get('constraints', {})
    plan = {'task_id': task_id, 'task_revision': task['revision'], 'origin': task['origin'],
            'destination': task['destination'], 'train': None, 'hotel': None,
            'schedule': dict(departure_at=None, arrival_at=None, check_in=None, check_out=None, next_departure_not_before=None)}
    if not isinstance(draft, dict) or set(draft) - {'task_revision', 'train_selection', 'hotel_selection', 'schedule', 'unverified_requirements'}:
        add('invalid_draft', '草稿包含不支持的字段。')
        return dict(valid=False, issues=issues, reconstructed_plan=plan)
    if draft.get('task_revision') != task['revision']:
        add('stale_draft', '草稿不属于当前任务版本。')
    schedule = draft.get('schedule') or {}
    if not isinstance(schedule, dict) or set(schedule) - set(plan['schedule']):
        add('invalid_schedule', '时间安排包含不支持的字段。'); schedule = {}
    for domain in ('train', 'hotel'):
        selection = draft.get(f'{domain}_selection')
        if domain == 'hotel' and not task['requires_hotel']:
            if selection is not None: add('unexpected_hotel', '返程任务不能增加酒店。')
            continue
        if not selection:
            add(f'{domain}_selection_missing', f'当前段尚无已核实的{domain}候选选择。')
            continue
        try:
            row = workflow['results_by_query'][selection['query_id']]
            if row['domain'] != domain or row['status'] not in {'ok', 'partial'}:
                raise ValueError('wrong domain or status')
            if row.get('needs_revalidation'):
                add('evidence_needs_revalidation', '报价或库存刷新未完成，旧事实不能作为最新保证。')
            item = resolve_selection(workflow, task_id, task['revision'], selection)
            item = _offer(item, domain).to_dict()
            parameters = row['parameters']
            if domain == 'train':
                if parameters.get('origin') != task['origin'] or parameters.get('destination') != task['destination']:
                    raise ValueError('train route mismatch')
                if parameters.get('passengers', 1) != effective['passengers']:
                    raise ValueError('train party mismatch')
                fixed_date = task['conditions'].get('departure_date')
                if fixed_date and parameters.get('departure_date') != fixed_date:
                    raise ValueError('train date mismatch')
                departure = item.get('departure_at')
                if not departure and parameters.get('departure_date') and item.get('departure_time'):
                    departure = parameters['departure_date']+'T'+item['departure_time']+':00+08:00'
                if departure and (datetime.fromisoformat(departure).date().isoformat() != parameters['departure_date'] or
                                  datetime.fromisoformat(departure).strftime('%H:%M') != item['departure_time']):
                    raise ValueError('departure evidence mismatch')
                arrival = item.get('arrival_at')
                if arrival and datetime.fromisoformat(arrival).strftime('%H:%M') != item['arrival_time']:
                    raise ValueError('arrival evidence mismatch')
                plan['schedule'].update(departure_at=departure, arrival_at=arrival)
                if not departure: add('departure_time_unknown', '火车出发时间未核实。')
                if not arrival: add('arrival_time_unknown', '抵达日期未核实，不能保证住宿与下一段衔接。')
                if item.get('availability') != 'available': add('train_inventory_unknown', '火车余票未确认或不可用。')
                if item.get('remaining') is not None and item['remaining'] < effective['passengers']:
                    add('train_inventory_insufficient', '余票不足以满足本段人数。')
                if item.get('price_cny') is None: add('train_price_unknown', '火车票价未核实。')
                if constraints.get('seat_class') and item.get('seat_class') != constraints['seat_class']:
                    add('seat_constraint_failed', '席别不符合用户要求。')
                price = Decimal(item['price_cny']) * effective['passengers'] if item.get('price_cny') is not None else None
                if constraints.get('train_max_total_cny') is not None and (price is None or price > Decimal(str(constraints['train_max_total_cny']))):
                    add('train_budget_failed', '本段火车费用未满足预算。')
            else:
                if parameters.get('city') != task['destination']:
                    raise ValueError('hotel city mismatch')
                if item.get('kind') == 'hotel_place':
                    if item.get('city') and item['city'] != task['destination']:
                        raise ValueError('hotel place city mismatch')
                    price_required = bool(effective.get('hotel_quote_required') or any(k.startswith('hotel_max_') for k in constraints) or 'total_budget_cny' in constraints)
                    add('hotel_price_unknown', '酒店地点不提供房价、空房或入住规则。', blocking=price_required)
                else:
                    if item.get('guests') != effective['guests']:
                        raise ValueError('hotel party mismatch')
                    if item.get('availability') != 'available' or item.get('fees_included') is not True:
                        add('hotel_inventory_unknown', '酒店报价或可用库存未完整核实。')
                    amount = Decimal(item['stay_total_cny']) if item.get('stay_total_cny') is not None else None
                    if amount is None: add('hotel_price_unknown', '酒店价格未知。')
                    for k in ('hotel_max_total_cny', 'hotel_max_nightly_cny'):
                        if k in constraints:
                            nights = (date.fromisoformat(item['check_out'])-date.fromisoformat(item['check_in'])).days
                            cost = amount / nights if amount is not None and k.endswith('nightly_cny') else amount
                            if cost is None or cost > Decimal(str(constraints[k])): add('hotel_budget_failed', '住宿费用不符合预算。')
            plan[domain] = item
        except (ValueError, KeyError, TypeError, ArithmeticError, AttributeError):
            add('invalid_candidate_reference', f'{domain}候选的归属、版本、人数、日期或来源不匹配。')
    if task['requires_hotel']:
        hotel = plan['hotel'] or {}
        for key in ('check_in', 'check_out'):
            value = task['conditions'].get(key) or schedule.get(key)
            if hotel.get('kind') != 'hotel_place' and hotel.get(key):
                if value and value != hotel[key]: add('hotel_date_mismatch', '住宿安排与报价日期不匹配。')
                value = hotel[key]
            if value:
                try: plan['schedule'][key] = date.fromisoformat(value).isoformat()
                except (ValueError, TypeError): add('invalid_hotel_dates', '住宿日期格式无效。')
            else: add('hotel_dates_missing', '住宿安排尚缺入住或离店日期。')
        start, end = plan['schedule']['check_in'], plan['schedule']['check_out']
        if start and end and start >= end: add('invalid_hotel_dates', '离店日期必须晚于入住日期。')
        arrival = plan['schedule']['arrival_at']
        if start and arrival and start < datetime.fromisoformat(arrival).date().isoformat():
            add('hotel_before_arrival', '入住日期早于火车实际抵达日期。')
        plan['schedule']['next_departure_not_before'] = end
    else:
        plan['schedule']['next_departure_not_before'] = plan['schedule']['arrival_at']
    return dict(valid=not any(i['blocking'] for i in issues), issues=issues, reconstructed_plan=plan)


def check_workflow(workflow):
    issues, plans, lines, missing = [], [], [], []
    for index, task in enumerate(workflow['tasks']):
        if task['status'] not in {'draft', 'validated'}:
            issues.append(issue('task_not_draft', [task['id']], '任务尚未形成当前版本草稿。'))
        checked = check_task(workflow, task['id'], task.get('draft_plan'))
        issues.extend(checked['issues']); plan = checked['reconstructed_plan']; plans.append(plan)
        if index:
            prev = workflow['tasks'][index-1]
            if task['origin'] != prev['destination'] or task['depends_on'] != [prev['id']]:
                issues.append(issue('route_dependency_mismatch', [prev['id'], task['id']], '路线或任务依赖不连续。'))
            previous = plans[index-1]['schedule']; depart = plan['schedule']['departure_at']
            if depart and previous['arrival_at'] and datetime.fromisoformat(depart) < datetime.fromisoformat(previous['arrival_at']):
                issues.append(issue('departure_before_previous_arrival', [prev['id'], task['id']], '下一段在前段抵达之前出发。'))
            if depart and previous['check_out'] and datetime.fromisoformat(depart).date().isoformat() < previous['check_out']:
                issues.append(issue('departure_before_previous_checkout', [prev['id'], task['id']], '下一段在前段离店日期之前出发。'))
        try:
            effective, _ = effective_conditions(workflow['confirmed_conditions'], task['conditions'], {})
            train = _offer(plan['train'], 'train') if plan['train'] else None
            hotel = _offer(plan['hotel'], 'hotel') if plan['hotel'] and plan['hotel'].get('kind') != 'hotel_place' else None
            budget = build_budget(train, hotel, effective['passengers'], {'train', 'hotel'} if task['requires_hotel'] else {'train'}).to_dict()
            lines.extend({**line, 'task_id': task['id']} for line in budget['lines'])
            missing.extend({'task_id': task['id'], 'category': category} for category in budget['missing_categories'])
        except (ValueError, TypeError, KeyError):
            missing.append({'task_id': task['id'], 'category': 'unverified'})
    subtotal = sum((Decimal(line['amount_cny']) for line in lines), Decimal('0'))
    maximum = workflow['confirmed_conditions'].get('constraints', {}).get('total_budget_cny')
    if maximum is not None:
        if missing: issues.append(issue('total_budget_unknown', [t['id'] for t in workflow['tasks']], '全程费用有未知部分，不能验证预算。'))
        elif subtotal > Decimal(str(maximum)):
            issues.append(issue('total_budget_exceeded', [t['id'] for t in workflow['tasks']], '全程火车及住宿费用超过预算。', evidence=str(subtotal)))
    return dict(valid=not any(i['blocking'] for i in issues), issues=issues, reconstructed_tasks=plans,
                budget=dict(known_subtotal_cny=str(subtotal), verified=not missing, missing_categories=missing, lines=lines),
                validated_revisions={t['id']: t['revision'] for t in workflow['tasks']}, signature=state_signature(workflow))


def finalize_workflow(workflow, check):
    if not check.get('valid') or check.get('signature') != state_signature(workflow) or not check_workflow(workflow)['valid']:
        raise ValueError('workflow has not passed current global validation')
    w = deepcopy(workflow)
    for task in w['tasks']: task['status'] = 'validated'
    w.update(status='completed', validation=deepcopy(check), stop_reason='validated', checkpoint=None)
    return w
