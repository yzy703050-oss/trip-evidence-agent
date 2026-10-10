"""Information-role tool executor: validates parameters and preserves real results."""
import asyncio
from copy import deepcopy
from time import perf_counter

from travel_data.agent_support import make_query
from travel_data.hotel_places import make_hotel_place_query
from travel_data.candidates import CandidateStore, query_cache_key, candidate_view
from travel_data.contracts import AgentDataResult
from travel_data.providers import UnavailableProvider
from travel_data.public_query import PublicQueryProvider
from travel_data.result_guard import guard_domain_result
from agents.contracts import validate_constraints

TOOL_DOMAINS = {'train_search': 'train', 'hotel_search': 'hotel', 'travel_guide': 'guide',
                'weather_query': 'weather', 'web_search': 'web', 'train_search_by_arrival': 'train'}
PARAMETERS = {
    'train_search': {'origin': 'string', 'destination': 'string', 'departure_date': 'string', 'passengers': 'integer'},
    'train_search_by_arrival': {'origin': 'string', 'destination': 'string', 'arrival_date': 'string',
                              'arrival_before': 'string', 'departure_date': 'string', 'passengers': 'integer'},
    'hotel_search': {'city': 'string', 'keywords': 'string', 'check_in': 'string', 'check_out': 'string', 'guests': 'integer'},
    'travel_guide': {'destination': 'string', 'visit_dates': 'array'},
    'weather_query': {'city': 'string', 'date': ['string', 'null']},
    'web_search': {'query': 'string'},
}
REQUIRED = {'train_search': ['origin', 'destination', 'departure_date'],
            'train_search_by_arrival': ['origin', 'destination', 'arrival_date'],
            'hotel_search': ['city', 'check_in', 'check_out', 'guests'],
            'travel_guide': ['destination'], 'weather_query': ['city'], 'web_search': ['query']}


class ToolExecutor:
    def __init__(self, providers, public_provider=None):
        self.providers = providers
        self.public_provider = public_provider or PublicQueryProvider()

    def schemas(self):
        place_search = getattr(self.providers.get('hotel_search'), 'hotel_places', False) is True
        return [{'type': 'function', 'function': {'name': name, 'description': (
            '搜索城市内的真实酒店地点候选，city 必填，keywords 可选；无需入住日期或人数。'
            '不提供房型、房价、空房或预算合格证明。' if name == 'hotel_search' and place_search
            else f'查询{TOOL_DOMAINS[name]}，只返回真实来源数据。'),
            'parameters': {'type': 'object', 'properties': {
                **{key: {'type': kind, **({'items': {'type': 'string'}} if kind == 'array' else {})} for key, kind in params.items()},
                'constraints': {'type': 'object', 'description': '本地硬约束：hotel_max_total_cny/hotel_max_nightly_cny/train_max_total_cny/seat_class/departure_time_after/departure_time_before/available_only'},
                'candidate_offset': {'type': 'integer', 'minimum': 0}, 'refresh': {'type': 'boolean'}},
                'required': ['city'] if name == 'hotel_search' and place_search else REQUIRED[name],
                'additionalProperties': False}}} for name, params in PARAMETERS.items()]

    async def execute(self, name, arguments, run, *, call_id, _date_retry=False):
        if run.workflow is not None:
            from agents.workflow_contracts import task_by_id, effective_conditions
            task = task_by_id(run.workflow, run.current_task_id)
            domain = TOOL_DOMAINS.get(name)
            effective, _ = effective_conditions(run.workflow['confirmed_conditions'], task['conditions'], run.effective_preferences)
            arguments = deepcopy(arguments) if isinstance(arguments, dict) else arguments
            invalid = domain not in {'train', 'hotel'} or not isinstance(arguments, dict)
            if not invalid:
                fixed_constraints = {k: v for k, v in effective.get('constraints', {}).items() if k != 'total_budget_cny'}
                local_constraints = arguments.get('constraints', {})
                if not isinstance(local_constraints, dict) or any(k in fixed_constraints and v != fixed_constraints[k] for k, v in local_constraints.items()):
                    invalid = True
                else:
                    arguments['constraints'] = {**fixed_constraints, **local_constraints}
                if domain == 'train':
                    invalid |= arguments.get('origin') != task['origin'] or arguments.get('destination') != task['destination']
                    fixed = task['conditions'].get('departure_date')
                    if fixed: arguments.setdefault('departure_date', fixed)
                    invalid |= bool(fixed and arguments.get('departure_date') != fixed)
                    for key in ('arrival_date', 'arrival_before'):
                        if effective.get(key):
                            invalid |= bool(arguments.get(key) and arguments[key] != effective[key])
                            if name == 'train_search_by_arrival': arguments.setdefault(key, effective[key])
                    arguments.setdefault('passengers', effective['passengers'])
                    invalid |= arguments['passengers'] != effective['passengers']
                else:
                    invalid |= not task['requires_hotel'] or arguments.get('city') != task['destination']
                    arguments.setdefault('guests', effective['guests'])
                    invalid |= arguments['guests'] != effective['guests']
                    for key in ('check_in', 'check_out'):
                        fixed = task['conditions'].get(key)
                        if fixed and task.get('field_sources', {}).get(key) in {'user', 'context'} and key in arguments:
                            invalid |= arguments[key] != fixed
            if invalid:
                return AgentDataResult('error', {}, [], [], None, None, '工具参数不属于当前任务或改变了确认条件。').to_dict()
        result = await self._execute(name, arguments, run, call_id=call_id)
        if not any(row['id'] == call_id for row in run.tool_requests):
            run.tool_requests.append({'id': call_id, 'name': name, 'status': result['status'],
                'cache_hit': False, 'elapsed_ms': 0, 'query': result.get('query', {})})
        if run.workflow is not None:
            from agents.workflow_queries import record_query
            domain = TOOL_DOMAINS[name]
            parameters = result.get('query') or {k: v for k, v in arguments.items() if k in PARAMETERS[name]}
            metadata = next(r for r in reversed(run.tool_requests) if r['id'] == call_id)
            full = result
            raw = run.candidates.cache.get(query_cache_key(domain, parameters)) if run.candidates else None
            if raw is not None and result['status'] in {'ok', 'partial'}:
                full = raw.to_dict()
                full['query'] = parameters
            row = record_query(run.workflow, run.current_task_id, parameters,
                               arguments.get('constraints', {}), full, metadata,
                               domain=domain, refresh=arguments.get('refresh') is True)
            run.query_records[row['id']] = deepcopy(row)
            row['view_offset'] = arguments.get('candidate_offset', 0)
            result.update(query_id=row['id'], result_revision=row['result_revision'], task_id=row['task_id'], task_revision=row['task_revision'])
            run.workflow['candidate_cache'] = run.candidates.snapshot() if run.candidates else {}
            # Only suggested dates may move, and only after a successful empty query.
            # Unavailable/error and unknown arrival evidence do not authorize retries.
            if (name == 'train_search' and not _date_retry and result['status'] == 'ok' and not result['items']
                    and task.get('field_sources', {}).get('departure_date') in {'default', 'proposal'}
                    and not effective.get('arrival_date') and not effective.get('arrival_before')):
                from datetime import date, timedelta
                original_date = task['conditions']['departure_date']
                original_source = task['field_sources']['departure_date']
                for days in (1, 2):
                    if run.workflow_tool_budget is not None:
                        if run.workflow_tool_budget <= 0: break
                        run.workflow_tool_budget -= 1
                    proposed = (date.fromisoformat(original_date)+timedelta(days=days)).isoformat()
                    if effective.get('end_date') and proposed > effective['end_date']: break
                    task['conditions']['departure_date'] = proposed
                    task['field_sources']['departure_date'] = 'proposal'
                    result = await self.execute(name, {**arguments, 'departure_date': proposed}, run,
                                                call_id=call_id+f':date{days}', _date_retry=True)
                    if result['items'] or result['status'] != 'ok': break
                if not result['items']:
                    task['conditions']['departure_date'] = original_date
                    task['field_sources']['departure_date'] = original_source
        return result

    async def _execute(self, name, arguments, run, *, call_id):
        started = perf_counter()
        domain = TOOL_DOMAINS.get(name)
        query, cached = {}, False
        def failure(status, fields=None, message='查询条件或工具无效。'):
            return AgentDataResult(status, query, [], fields or [], None, None, message).to_dict()
        if domain is None or not isinstance(arguments, dict):
            return failure('error')
        if set(arguments) - (set(PARAMETERS[name]) | {'constraints', 'candidate_offset', 'refresh'}):
            return failure('error')
        offset = arguments.get('candidate_offset', 0)
        constraints = arguments.get('constraints', {})
        if type(offset) is not int or offset < 0 or not isinstance(constraints, dict):
            return failure('error')
        if run.workflow is not None:
            from agents.workflow_contracts import task_by_id, effective_conditions
            task = task_by_id(run.workflow, run.current_task_id)
            effective, _ = effective_conditions(run.workflow['confirmed_conditions'], task['conditions'], run.effective_preferences)
            previous = {k: v for k, v in effective.get('constraints', {}).items() if k != 'total_budget_cny'}
        else:
            previous = run.travel_conditions.get('constraints', {})
        if any(key in previous and previous[key] != value for key, value in constraints.items()):
            return failure('error', message='不能修改已确认的硬约束。')
        constraints = {**previous, **constraints}
        try:
            validate_constraints(constraints)
            if 'refresh' in arguments and type(arguments['refresh']) is not bool:
                raise ValueError('invalid refresh')
            if domain in {'train', 'hotel', 'guide'}:
                provider = self.providers.get('train_search' if name == 'train_search_by_arrival' else name) or UnavailableProvider()
                if name == 'train_search_by_arrival':
                    from travel_data.arrival_search import arrival_parameters, search_by_arrival
                    query = arrival_parameters(arguments)
                    typed, missing = None, []
                    arrival_used = [0]
                    def count_arrival_request():
                        if run.workflow is not None:
                            used = run.external_requests_by_task.get(run.current_task_id, 0)
                            if used >= 10: return False
                            if arrival_used[0]:
                                if run.workflow_tool_budget is not None and run.workflow_tool_budget <= 0: return False
                                if run.workflow_tool_budget is not None: run.workflow_tool_budget -= 1
                            run.external_requests_by_task[run.current_task_id] = used+1
                        arrival_used[0] += 1
                        run.external_request_count += 1
                        return True
                    fetch = lambda: search_by_arrival(provider, {**arguments, 'constraints': constraints},
                                                      request_budget=3, on_request=count_arrival_request)
                elif domain == 'hotel' and getattr(provider, 'hotel_places', False) is True:
                    typed, missing = make_hotel_place_query(arguments)
                else:
                    typed, missing = make_query(domain, arguments)
                if missing:
                    result = failure('needs_input', missing)
                    run.domain_results[domain] = result
                    return result
                if name != 'train_search_by_arrival':
                    query = typed.to_dict()
                    fetch = lambda: provider.search(typed)
            else:
                missing = [key for key in REQUIRED[name] if not isinstance(arguments.get(key), str) or not arguments[key].strip()]
                if missing:
                    result = failure('needs_input', missing)
                    run.domain_results[domain] = result
                    return result
                query = {key: arguments.get(key) for key in PARAMETERS[name]}
                if domain == 'weather' and query.get('date'):
                    from datetime import date
                    date.fromisoformat(query['date'])
                fetch = (lambda: self.public_provider.weather(query['city'], query.get('date'))) if domain == 'weather' else (lambda: self.public_provider.web(query['query']))
            if run.feedback_round and any(key in run.travel_conditions and run.travel_conditions[key] != val
                    for key, val in query.items() if val is not None):
                return failure('error', message='补查不能修改已确认的查询条件。')
            if run.workflow is None:
                run.travel_conditions['constraints'] = constraints
                run.travel_conditions.update({key: value for key, value in query.items() if value is not None})
            if run.candidates is None:
                run.candidates = CandidateStore()
            key = query_cache_key(domain, query)
            async def request():
                run.external_requests_started = True
                try:
                    if name != 'train_search_by_arrival':
                        if run.workflow is not None:
                            used = run.external_requests_by_task.get(run.current_task_id, 0)
                            if used >= 10: return AgentDataResult('partial', query, [], [], None, None, '当前任务外部查询预算已用尽。')
                            run.external_requests_by_task[run.current_task_id] = used+1
                        run.external_request_count += 1
                    data = await asyncio.wait_for(fetch(), run.limits.tool_timeout)
                    if not isinstance(data, AgentDataResult):
                        raise TypeError('provider result type')
                    if data.query != query:
                        raise ValueError('provider query mismatch')
                    checked = guard_domain_result(domain, data.to_dict())
                    return AgentDataResult(checked['status'], data.query, checked['items'],
                        data.missing_fields, data.source, data.fetched_at, checked.get('message'))
                except Exception:
                    return AgentDataResult('error', query, [], [], None, None, '数据查询失败或超时。')
            existing = run.candidates.cache.get(key)
            if run.workflow is not None and existing is not None and existing.status in {'unavailable', 'error'}:
                # No reliable transient-failure evidence: do not repeat the same failed request.
                raw, cached = existing, True
            else:
                raw, cached = await run.candidates.get_or_fetch(key, request, refresh=arguments.get('refresh') is True)
            if cached and query.get('search_kind') == 'hotel_place' and raw.query != query:
                raw = AgentDataResult(raw.status, query, raw.items, raw.missing_fields, raw.source, raw.fetched_at, raw.message)
            result = guard_domain_result(domain, candidate_view(raw, limit=run.limits.candidate_limit,
                offset=offset, constraints=constraints, preferences=run.effective_preferences))
        except (ValueError, TypeError, ArithmeticError):
            result = failure('needs_input', ['query'], '请修正查询条件。')
        run.domain_results[domain] = deepcopy(result)
        run.tool_requests.append({'id': call_id, 'name': name, 'status': result['status'],
            'cache_hit': cached, 'elapsed_ms': round((perf_counter()-started)*1000, 3), 'query': query})
        return result
