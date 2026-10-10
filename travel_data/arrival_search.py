"""Bounded arrival-date search over departure-date providers."""
from datetime import date, datetime, timedelta, timezone

from travel_data.contracts import AgentDataResult, TrainQuery
from travel_data.candidates import candidate_view

BEIJING = timezone(timedelta(hours=8))


def arrival_parameters(fields):
    if not all(isinstance(fields.get(k), str) and fields[k].strip() for k in ('origin', 'destination', 'arrival_date')):
        raise ValueError('arrival search needs route and date')
    date.fromisoformat(fields['arrival_date'])
    passengers = fields.get('passengers', 1)
    if type(passengers) is not int or passengers < 1: raise ValueError('invalid passengers')
    result = dict(origin=fields['origin'], destination=fields['destination'],
                  arrival_date=fields['arrival_date'], passengers=passengers, search_kind='train_arrival')
    for key in ('departure_date', 'arrival_before'):
        if fields.get(key):
            if key == 'departure_date': date.fromisoformat(fields[key])
            else:
                stamp = datetime.fromisoformat(fields[key])
                if stamp.tzinfo is None: raise ValueError('deadline needs timezone')
            result[key] = fields[key]
    return result


async def search_by_arrival(provider, fields, *, request_budget=3, on_request=None):
    query = arrival_parameters(fields)
    target = date.fromisoformat(query['arrival_date'])
    dates = [date.fromisoformat(query['departure_date'])] if query.get('departure_date') else [target-timedelta(days=i) for i in range(3)]
    last, unknown, used = None, False, 0
    deadline = datetime.fromisoformat(query['arrival_before']) if query.get('arrival_before') else None
    for day in dates:
        if used >= request_budget: break
        if on_request is not None and not on_request(): break
        used += 1
        last = await provider.search(TrainQuery(query['origin'], query['destination'], day, query['passengers']))
        if last.status in {'unavailable', 'error'}:
            return AgentDataResult(last.status, query, [], last.missing_fields, last.source, last.fetched_at, last.message)
        items = []
        for item in last.items:
            arrival, departure = item.get('arrival_at'), item.get('departure_at')
            if not arrival or not departure:
                unknown = True
                continue
            at = datetime.fromisoformat(arrival).astimezone(BEIJING)
            dep = datetime.fromisoformat(departure).astimezone(BEIJING)
            if dep.date() != day or at.date() != target or (deadline and at > deadline): continue
            if at.strftime('%H:%M') != item.get('arrival_time') or at < dep: continue
            items.append(item)
        filtered = candidate_view(AgentDataResult(last.status, last.query, items, [], last.source, last.fetched_at, last.message),
                                  limit=max(1, len(items)), offset=0, constraints=fields.get('constraints', {}), preferences={})['items']
        if filtered:
            return AgentDataResult('ok' if last.status == 'ok' else 'partial', query, filtered, [], last.source, last.fetched_at,
                                   f'按目标抵达日期筛选，已查询{used}个出发日期；来源见候选。')
        # A provider that only returns clocks cannot fix the missing date by changing dates.
        if unknown: break
    message = ('车次抵达日期证据不足。' if unknown else
               f'已查{used}个出发日期，未取得符合目标抵达日期的候选；搜索范围有限。')
    return AgentDataResult('partial' if unknown or used < len(dates) else 'ok', query, [], [],
                           last.source if last else None, last.fetched_at if last else None, message)
