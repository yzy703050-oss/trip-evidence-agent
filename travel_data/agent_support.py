"""Deterministic query extraction shared by sourced domain agents."""
import asyncio
import json
from datetime import date, timedelta

from agentscope.message import Msg
from travel_data.contracts import AgentDataResult, TrainQuery, HotelQuery, GuideQuery


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def confirmed_fields(msg):
    payload = _object(msg.content if msg is not None else None)
    fields = dict(_object(payload.get('context')))
    # Explicit collector data takes precedence. Never search nested profile/memory.
    previous = payload.get('previous_results', [])
    if isinstance(previous, list):
        for result in previous:
            result = _object(result)
            collected = _object(result.get('result')) if 'result' in result else result
            if result.get('agent_name') == 'event_collection' and collected.get('status') == 'success':
                fields.update(_object(collected.get('data')))
    return fields


def make_query(domain, fields):
    aliases = {'departure_date': 'start_date', 'city': 'destination'}
    required = {
        'train': ('origin', 'destination', 'departure_date'),
        'hotel': ('city', 'check_in', 'check_out', 'guests'),
        'guide': ('destination',),
    }[domain]
    values, missing = {}, []
    for key in required:
        value = fields.get(key, fields.get(aliases.get(key)))
        try:
            if key in {'departure_date', 'check_in', 'check_out'}:
                value = date.fromisoformat(value) if isinstance(value, str) else value
                if type(value) is not date:
                    raise ValueError()
            elif key == 'guests':
                if type(value) is not int or value < 1:
                    raise ValueError()
            elif not isinstance(value, str) or not value.strip():
                raise ValueError()
            values[key] = value
        except (ValueError, TypeError):
            missing.append(key)
    if missing:
        return None, missing
    if domain == 'train':
        passengers = fields.get('passengers')
        passengers = 1 if passengers is None else passengers
        if type(passengers) is not int or passengers < 1:
            return None, ['passengers']
        return TrainQuery(**values, passengers=passengers), []
    if domain == 'hotel':
        if values['check_out'] <= values['check_in']:
            return None, ['check_out']
        return HotelQuery(**values), []
    dates = fields.get('visit_dates')
    if dates is None:
        dates = []
        if fields.get('start_date'):
            try:
                start = date.fromisoformat(fields['start_date']) if isinstance(fields['start_date'], str) else fields['start_date']
                end_value = fields.get('end_date') or start
                end = date.fromisoformat(end_value) if isinstance(end_value, str) else end_value
                if type(start) is not date or type(end) is not date or end < start:
                    raise ValueError()
                dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
            except (ValueError, TypeError):
                return None, ['visit_dates']
    try:
        if not isinstance(dates, list):
            raise ValueError()
        dates = [date.fromisoformat(item) if isinstance(item, str) else item for item in dates]
        if any(type(item) is not date for item in dates):
            raise ValueError()
    except (ValueError, TypeError):
        return None, ['visit_dates']
    return GuideQuery(**values, visit_dates=dates), []


async def sourced_reply(agent, msg, domain):
    query, missing = make_query(domain, confirmed_fields(msg))
    if missing:
        result = AgentDataResult('needs_input', {}, [], missing, None, None,
                                 '请补充或修正查询条件。')
    else:
        try:
            # Bound a hung provider as well as handling explicit timeout failures.
            result = await asyncio.wait_for(agent.provider.search(query), timeout=30)
            if not isinstance(result, AgentDataResult):
                raise TypeError('provider result must be AgentDataResult')
        except TimeoutError:
            result = AgentDataResult('error', query.to_dict(), [], [], None, None,
                                     '数据接口查询超时；无法确认价格或库存。')
        except Exception:
            # Provider exception text can contain credentials; do not echo it.
            result = AgentDataResult('error', query.to_dict(), [], [], None, None,
                                     '数据接口查询失败；无法确认报价或事实。')
    return Msg(agent.name, json.dumps(result.to_dict(), ensure_ascii=False), 'assistant')
