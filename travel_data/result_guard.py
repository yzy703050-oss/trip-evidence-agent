"""Rebuild facts from validated sources for both forwarded and synthesized output."""
from copy import deepcopy
from datetime import date, datetime
from urllib.parse import urlparse
import math

from travel_data.contracts import Source, GuideFact
from travel_data.plan_guard import _offer


def valid_source(value):
    try:
        return Source(value['provider'], datetime.fromisoformat(value['fetched_at']), value.get('url'))
    except (KeyError, TypeError, ValueError):
        return None


def valid_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in {'http', 'https'} and bool(parsed.hostname)
    except (ValueError, TypeError):
        return False


def guard_domain_result(domain: str, result: dict) -> dict:
    value = deepcopy(result)
    items, skipped = [], False
    for item in value.get('items', []):
        try:
            if domain in {'train', 'hotel'}:
                if domain == 'hotel' and any(key in value.get('query', {}) and item.get(key) != value['query'][key]
                                           for key in ('check_in', 'check_out', 'guests')):
                    raise ValueError('hotel stay mismatch')
                items.append(_offer(item, domain).to_dict())
            elif domain == 'guide':
                fact = GuideFact(item['kind'], item['content'], item['verification'], valid_source(item.get('source')))
                items.append(fact.to_dict())
                skipped |= fact.verification != 'verified'
            else:
                source = valid_source(item.get('source')) or valid_source(value.get('source'))
                if source is None or not valid_url(source.url):
                    raise ValueError('source missing')
                if domain == 'weather':
                    if item.get('date'):
                        date.fromisoformat(item['date'])
                    requested = value.get('query', {}).get('date')
                    if requested and item.get('date') != requested:
                        raise ValueError('weather date mismatch')
                    temperatures = [item[k] for k in ('temp_c', 'min_temp_c', 'max_temp_c') if item.get(k) is not None]
                    if not temperatures or any(isinstance(t, bool) or not math.isfinite(float(t)) for t in temperatures):
                        raise ValueError('invalid temperature')
                    if item.get('min_temp_c') is not None and item.get('max_temp_c') is not None and float(item['min_temp_c']) > float(item['max_temp_c']):
                        raise ValueError('invalid temperature range')
                if domain == 'web' and not valid_url(item.get('url')):
                    raise ValueError('invalid search source')
                items.append({**item, 'source': source.to_dict()})
        except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
            skipped = True
    value['items'] = items
    if value.get('status') not in {'ok', 'partial', 'needs_input', 'unavailable', 'error'}:
        value['status'] = 'error'
    if (skipped or (domain == 'weather' and not items)) and value['status'] == 'ok':
        value['status'] = 'partial'
        value['message'] = '部分数据缺少有效来源或字段，已排除。'
    return value


def guard_information_result(value: dict, requested_domains: list[str]) -> dict:
    result = deepcopy(value)
    result['domain_results'] = {domain: guard_domain_result(domain, data)
        for domain, data in result.get('domain_results', {}).items() if domain in {'train', 'hotel', 'guide', 'weather', 'web'}}
    missing = set(result.get('missing_fields', []))
    statuses = []
    for domain in requested_domains:
        data = result['domain_results'].get(domain)
        if data is None:
            statuses.append('partial')
        else:
            statuses.append(data['status'])
            missing.update(data.get('missing_fields', []))
    result['missing_fields'] = sorted(missing)
    if missing:
        status = 'needs_input'
    elif statuses and all(s == 'ok' for s in statuses):
        status = 'ok'
    elif statuses and len(set(statuses)) == 1:
        status = statuses[0]
    else:
        status = 'partial'
    if result.get('status') == 'error' and status == 'ok':
        status = 'partial'
    result['status'] = status
    result.setdefault('summary', '')
    result.setdefault('travel_conditions', {})
    return result


def grounded_answer(info: dict) -> str:
    """Financial and weather facts come from structured data, never model prose."""
    domains = info.get('domain_results', {})
    lines = []
    if 'weather' in domains:
        lines = []
        data = domains['weather']
        city = data.get('query', {}).get('city', '')
        for item in data.get('items', []):
            period = item.get('date') or '当前'
            if item.get('min_temp_c') is not None and item.get('max_temp_c') is not None:
                lines.append(f"{city}{period}：{item.get('description', '')}，{item['min_temp_c']}～{item['max_temp_c']}℃。")
            elif item.get('temp_c') is not None:
                lines.append(f"{city}{period}：{item.get('description', '')}，{item['temp_c']}℃。")
        if not lines:
            lines.append(data.get('message') or '没有可核实的天气数据。')
    if {'train', 'hotel', 'guide'} & set(domains):
        # The CLI renders sourced offers in detail. Keep model-generated money
        # and inventory assertions out of the accompanying free-text response.
        labels = {'train': '火车', 'hotel': '酒店', 'guide': '攻略', 'weather': '天气', 'web': '网页'}
        lines.extend(f"{labels[d]}查询：{len(data.get('items', []))} 个已取得结果；{data.get('message') or data.get('status', '')}。"
                     for d, data in domains.items() if d in {'train', 'hotel', 'guide'})
        lines.extend(f"{item['content']}（{item['verification']}）" for item in domains.get('guide', {}).get('items', []))
    if lines:
        if 'web' in domains:
            lines.extend(f"{item.get('title', '')}：{item.get('snippet', '')}" for item in domains['web'].get('items', []))
        return '\n'.join(lines)
    return info.get('summary', '')
