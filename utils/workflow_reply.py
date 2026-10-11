"""Let the main model compose prose around immutable, validated travel facts."""

import re
from datetime import date

from utils.response_renderer import _format_date, _format_datetime, _offer

TOKEN = re.compile(r'\{\{([a-z][a-z0-9_]*)\}\}')
# Facts belong in placeholders; free prose is for transitions and suggestions.
UNSUPPORTED = re.compile(
    r'[0-9A-Za-z]|[零一二三四五六七八九十百千万两]+(?:元|点|时|分|晚|天|号|日|月|折|座|次)'
    r'|已(?:预订|订房|订好|购票|出票|支付)|预订成功|免费|保证|一定能'
    r'|没有火车|无车|有票|空房充足|已核实|非用户确认|系统建议|草稿|来源|查询时间'
)


def build_reply_facts(result):
    """Build required public values; never pass provider payloads to the writer."""
    values, paragraphs = {}, []
    checked = result.get('validated_plan') or {}
    workflow = result.get('workflow') or {}
    originals = {t['id']: t for t in workflow.get('tasks', [])}
    hotel_unknown = False
    train_missing = False
    for index, plan in enumerate(checked.get('reconstructed_tasks', []), 1):
        prefix = f's{index}_'
        original = originals.get(plan.get('task_id'), {})
        conditions = original.get('conditions') or {}
        schedule = plan.get('schedule') or {}
        train, hotel = plan.get('train'), plan.get('hotel')
        label = '回程' if plan.get('purpose') == 'return' else ('去程' if index == 1 else '下一段')
        route = f"{plan.get('origin') or '待确认的出发地'}到{plan.get('destination') or '待确认的目的地'}"
        if train:
            route = f"{train.get('origin_station') or plan.get('origin')}到{train.get('destination_station') or plan.get('destination')}"
        values[prefix + 'route'] = route
        sentence = f'{label}从{route}'
        if train:
            values[prefix + 'train'] = train.get('train_number') or '该车次'
            sentence += f"，可选{values[prefix + 'train']}"
            for field, label_text, suffix in [('departure_at', 'departure', '出发'),
                                              ('arrival_at', 'arrival', '抵达')]:
                stamp = train.get(field) or schedule.get(field) or train.get(label_text + '_time')
                if stamp:
                    values[prefix + label_text] = _format_datetime(stamp)
                    sentence += f"，{values[prefix + label_text]}{suffix}"
            fare = (f"单人票价{train['price_cny']}元" if train.get('price_cny') is not None
                    else '票价还未确认')
            values[prefix + 'fare'] = (train.get('seat_class') or '') + fare
            sentence += '，' + values[prefix + 'fare']
            stock = {'available': '查询时有票', 'unavailable': '查询时无票'}.get(
                train.get('availability'), '余票还未确认')
            if train.get('remaining') is not None:
                stock += f"，余票{train['remaining']}张"
            if train.get('needs_revalidation'):
                stock += '；这是此前取得的信息，价格和余票需要重新核实'
            values[prefix + 'stock'] = stock
            sentence += '，' + stock
            if not train.get('arrival_at') and not schedule.get('arrival_at'):
                values[prefix + 'arrival_gap'] = '抵达日期还未确认，住宿日期需要随实际抵达调整'
                sentence += '；' + values[prefix + 'arrival_gap']
        else:
            train_missing = True
            rows = [r for r in workflow.get('results_by_query', {}).values()
                    if r.get('task_id') == plan.get('task_id') and r.get('domain') == 'train']
            gap = ('这次查询范围内没找到合适车次，发到时刻和票价还不能确定'
                   if rows and rows[-1].get('status') == 'ok'
                   else '车次这次没查到，发到时刻和票价还不能确定')
            values[prefix + 'train_gap'] = gap
            sentence += '，' + gap
            planned = conditions.get('departure_date')
            if planned:
                values[prefix + 'planned_departure'] = '计划' + _format_date(planned) + '出发'
                sentence += '；' + values[prefix + 'planned_departure']
        start = schedule.get('check_in') or conditions.get('check_in')
        end = schedule.get('check_out') or conditions.get('check_out')
        nights = conditions.get('nights')
        if not nights and start and end:
            try:
                nights = (date.fromisoformat(end) - date.fromisoformat(start)).days
            except ValueError:
                pass
        if (hotel and hotel.get('kind') == 'hotel_place') or original.get('requires_hotel'):
            stay = []
            if nights:
                source = (original.get('field_sources') or {}).get('nights', 'proposal')
                stay.append(('住' if source in {'user', 'context'} else '建议住') + f'{nights}晚')
            if start:
                stay.append(_format_date(start) + '入住')
            if end:
                stay.append(_format_date(end) + '离店')
            if stay:
                values[prefix + 'stay'] = '，'.join(stay)
                sentence += '。住宿' + values[prefix + 'stay']
        if hotel:
            if hotel.get('kind') == 'hotel_place':
                hotel_unknown = True
                name = hotel.get('hotel_name') or '该住宿地点'
                if hotel.get('address'):
                    name += '（' + hotel['address'] + '）'
                values[prefix + 'hotel'] = name
                sentence += '，可以考虑' + name
            else:
                quote = dict(hotel)
                if start:
                    quote.pop('check_in', None)
                if end:
                    quote.pop('check_out', None)
                values[prefix + 'hotel'] = '；'.join(_offer(quote, 'hotel'))
                sentence += '。' + values[prefix + 'hotel']
        elif original.get('requires_hotel'):
            values[prefix + 'hotel_gap'] = '住宿暂未找到合适候选，可以按以上日期继续筛选'
            sentence += '。' + values[prefix + 'hotel_gap']
        paragraphs.append(sentence + '。')
    budget = checked.get('budget') or {}
    if budget.get('lines'):
        values['cost'] = f"已查到的费用合计{budget['known_subtotal_cny']}元"
        if not budget.get('verified'):
            values['cost'] += '，其余费用还需确认'
    if hotel_unknown:
        values['hotel_booking'] = '酒店房价、空房和入住及退改规则需在订房时确认'
    if train_missing:
        values['next_step'] = '可以先在铁路官方售票渠道核对车次，再确定发到时间；住宿日期也需随实际抵达调整'
    issues = {i.get('code') for i in result.get('gaps', []) if isinstance(i, dict)}
    conflict_codes = {'hotel_before_arrival', 'departure_before_previous_arrival',
                      'departure_before_previous_checkout', 'start_date_conflict',
                      'end_date_conflict', 'invalid_hotel_dates', 'stay_nights_mismatch'}
    if issues & conflict_codes:
        values['schedule_gap'] = '部分日期存在冲突，住宿与后续交通需要调整后才能衔接'
    if any(code and ('budget' in code or 'quote' in code) for code in issues):
        values['budget_gap'] = '当前费用信息还不能满足预算或报价要求，需要继续核对'
    if result.get('stop_reason') in {
            'main_decision_limit', 'task_information_limit', 'workflow_tool_limit', 'turn_timeout'}:
        values['continuation'] = '已有安排先保留，剩余内容可以接着查询或调整'
    if result.get('data_mode') == 'simulation':
        values['simulation'] = '以下为模拟数据，仅用于测试'
    summaries = [value for key, value in values.items() if not re.match(r's\d+_', key)]
    fallback = '\n\n'.join(paragraphs + ([' '.join(v + '。' for v in summaries)] if summaries else []))
    return {'values': values, 'required': list(values),
            'fallback': fallback or '暂时没有查到可用安排，可以保留需求稍后继续查询。'}


def render_reply_template(template, facts):
    if not isinstance(template, str) or not template.strip() or len(template) > 6000:
        raise ValueError('invalid reply template')
    keys = TOKEN.findall(template)
    required = facts['required']
    if set(keys) != set(required) or len(keys) != len(required):
        raise ValueError('reply must include each known fact exactly once')
    prose = TOKEN.sub('', template)
    if '{' in prose or '}' in prose or UNSUPPORTED.search(prose):
        raise ValueError('reply contains unsupported factual claims')
    rendered = TOKEN.sub(lambda m: facts['values'][m[1]], template)
    return '\n\n'.join(' '.join(part.split()) for part in rendered.splitlines() if part.strip())


def fallback_workflow_reply(result):
    return build_reply_facts(result)['fallback']
