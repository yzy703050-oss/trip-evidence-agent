"""Identify evidenced hard date conflicts, never infer impossibility from missing data."""

from datetime import date, datetime

from agents.condition_strengths import condition_is_hard, global_is_hard


def _conflict(tasks, description, fields, evidence=()):
    return {'code': 'hard_date_conflict', 'affected_task_ids': [t['id'] for t in tasks],
            'missing_fields': [], 'condition_fields': list(fields),
            'evidence_refs': list(evidence), 'reason': description,
            'question': description + '这些条件目前无法同时满足，你愿意调整哪项日期或到达期限？'}


def _trains(workflow, task):
    rows = []
    for row in workflow.get('results_by_query', {}).values():
        p = row.get('parameters', {})
        if (row.get('domain') != 'train' or row.get('task_id') != task['id']
                or row.get('task_revision') != task['revision']
                or row.get('status') not in {'ok', 'partial'} or row.get('needs_revalidation')
                or p.get('origin') != task['origin'] or p.get('destination') != task['destination']
                or p.get('passengers', 1) != task['conditions'].get(
                    'passengers', workflow.get('confirmed_conditions', {}).get('passengers', 1))):
            continue
        fixed = task['conditions'].get('departure_date')
        if fixed and p.get('departure_date') != fixed:
            continue
        rows.append(row)
    return rows


def find_hard_date_conflict(workflow):
    tasks, confirmed = workflow['tasks'], workflow.get('confirmed_conditions', {})
    if (global_is_hard(workflow, 'start_date') and global_is_hard(workflow, 'end_date')
            and confirmed['start_date'] > confirmed['end_date']):
        return _conflict(tasks, '指定出发日晚于最晚返回日。', ['start_date', 'end_date'])
    for index, task in enumerate(tasks):
        c = {**confirmed, **task['conditions']}
        departure = c.get('departure_date') or (confirmed.get('start_date') if index == 0 else None)
        hard_departure = departure and condition_is_hard(workflow, task, 'departure_date')
        deadline, arrival_date = c.get('arrival_before'), c.get('arrival_date')
        if hard_departure:
            target = deadline[:10] if deadline and condition_is_hard(workflow, task, 'arrival_before') else None
            if arrival_date and condition_is_hard(workflow, task, 'arrival_date'):
                target = min(target, arrival_date) if target else arrival_date
            if target and departure > target:
                return _conflict([task], f'指定出发日 {departure} 晚于要求抵达日 {target}。',
                                 ['departure_date', 'arrival_date', 'arrival_before'])
        if (c.get('check_in') and c.get('check_out')
                and condition_is_hard(workflow, task, 'check_in')
                and condition_is_hard(workflow, task, 'check_out')
                and c['check_in'] >= c['check_out']):
            return _conflict([task], '固定离店日期不晚于固定入住日期。', ['check_in', 'check_out'])
        if index and hard_departure:
            previous = tasks[index - 1]
            checkout = previous['conditions'].get('check_out')
            if checkout and condition_is_hard(workflow, previous, 'check_out') and departure < checkout:
                return _conflict([previous, task], f'固定下一段出发日 {departure} 早于固定离店日 {checkout}。',
                                 ['check_out', 'departure_date'])
        if not hard_departure:
            continue
        hard_deadline = deadline and condition_is_hard(workflow, task, 'arrival_before')
        hard_arrival = arrival_date and condition_is_hard(workflow, task, 'arrival_date')
        hard_end = index == len(tasks) - 1 and global_is_hard(workflow, 'end_date')
        if not (hard_deadline or hard_arrival or hard_end):
            continue
        rows = _trains(workflow, task)
        items = [item for row in rows for item in row.get('items', [])]
        if not items or any(not item.get('arrival_at') or not item.get('departure_at') for item in items):
            continue
        try:
            arrivals = [datetime.fromisoformat(item['arrival_at']) for item in items]
            departures = [datetime.fromisoformat(item['departure_at']) for item in items]
            if any(a.tzinfo is None or d.tzinfo is None or a <= d
                   or d.date() != date.fromisoformat(departure)
                   or a.strftime('%H:%M') != item.get('arrival_time')
                   or d.strftime('%H:%M') != item.get('departure_time')
                   for a, d, item in zip(arrivals, departures, items)):
                continue
            possible = any(
                (not hard_deadline or at <= datetime.fromisoformat(deadline))
                and (not hard_arrival or at.date().isoformat() == arrival_date)
                and (not hard_end or at.date().isoformat() <= confirmed['end_date'])
                for at in arrivals)
        except (ValueError, TypeError):
            continue
        if not possible:
            bound = deadline or arrival_date or confirmed['end_date']
            return _conflict([task], f'本次已查车次按固定日期 {departure} 出发均无法满足抵达期限 {bound}。',
                             ['departure_date', 'arrival_before', 'arrival_date', 'end_date'],
                             [row['id'] for row in rows])
    return None
