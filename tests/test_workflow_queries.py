from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import pytest
from agents.contracts import RunState
from agents.workflow_queries import record_query, query_views, resolve_selection
from travel_data.candidates import CandidateStore
from travel_data.contracts import AgentDataResult, Source
from travel_data.tools import ToolExecutor
from test_information_agent_loop import info_class
from workflow_support import workflow

SOURCE = {'provider': 'offline', 'fetched_at': '2026-10-10T10:00:00+08:00', 'url': 'https://example.com'}


def train_result(task, count=8):
    p = {'origin': task['origin'], 'destination': task['destination'],
         'departure_date': task['conditions']['departure_date'], 'passengers': 1}
    return {'status': 'ok', 'query': p, 'source': SOURCE, 'fetched_at': SOURCE['fetched_at'], 'missing_fields': [],
            'items': [{'id': f"{p['departure_date']}:G{i}", 'train_number': f'G{i}',
                       'origin_station': p['origin'], 'destination_station': p['destination'],
                       'departure_time': '08:00', 'arrival_time': '12:00', 'seat_class': '二等座',
                       'price_cny': '100', 'availability': 'available', 'remaining': 8, 'source': SOURCE, 'url': None,
                       'departure_at': p['departure_date']+'T08:00:00+08:00',
                       'arrival_at': p['departure_date']+'T12:00:00+08:00',
                       'time_evidence': {'departure': 'provider_datetime', 'arrival': 'provider_datetime'}} for i in range(count)]}


def put_train(w, index=0):
    t = w['tasks'][index]; data = train_result(t)
    return record_query(w, t['id'], data['query'], {}, data, {}, domain='train')


def test_queries_do_not_overwrite_the_route_and_five_is_only_a_view():
    w = workflow()
    records = [put_train(w, i) for i in range(3)]
    assert len({r['id'] for r in records}) == 3
    assert len(w['results_by_query']) == 3
    assert w['confirmed_conditions']['start_date'] == '2026-10-11'
    assert len(records[0]['items']) == 8
    views = query_views(w, w['tasks'][0]['id'])
    assert len(views[0]['items']) == 5
    assert len(query_views(w, w['tasks'][0]['id'], offset=5)[0]['items']) == 3


def test_selection_requires_matching_task_query_revision_and_id():
    w = workflow(); r = put_train(w); other = put_train(w, 1); t = w['tasks'][0]
    ref = dict(query_id=r['id'], result_revision=1, candidate_id=r['items'][0]['id'])
    assert resolve_selection(w, t['id'], 1, ref)['price_cny'] == '100'
    for bad in [dict(ref, query_id=other['id']), dict(ref, result_revision=2), dict(ref, candidate_id='fake')]:
        with pytest.raises(ValueError): resolve_selection(w, t['id'], 1, bad)


def test_failed_refresh_retains_facts_and_marks_reconfirmation():
    w = workflow(); r = put_train(w); task = w['tasks'][0]
    failed = dict(status='error', items=[], source=None, fetched_at=None)
    saved = record_query(w, task['id'], r['parameters'], {}, failed, {}, domain='train', refresh=True)
    assert saved['id'] == r['id'] and saved['items'] == r['items']
    assert saved['result_revision'] == 1 and saved['needs_revalidation'] is True
    fresh = record_query(w, task['id'], r['parameters'], {}, train_result(task), {}, domain='train', refresh=True)
    assert fresh['result_revision'] == 2
    assert fresh['needs_revalidation'] is False


def test_cache_snapshot_restores_sourced_candidates_without_request():
    cache = CandidateStore(); src = Source('offline', datetime.now(timezone.utc), 'https://example.com')
    cache.cache['hotel:test'] = AgentDataResult('ok', {'city': '北京'}, [{'id': 'p'}], [], src, src.fetched_at, None)
    restored = CandidateStore(); restored.restore(json.loads(json.dumps(cache.snapshot())))
    assert restored.cache['hotel:test'].to_dict() == cache.cache['hotel:test'].to_dict()


@pytest.mark.asyncio
async def test_workflow_info_only_executes_train_hotel_and_does_not_mutate_confirmed():
    w = workflow(); t = w['tasks'][0]; raw = train_result(t)
    class Provider:
        async def search(self, query):
            return AgentDataResult('ok', query.to_dict(), raw['items'], [],
                                   Source('offline', datetime.fromisoformat(SOURCE['fetched_at']), SOURCE['url']),
                                   datetime.fromisoformat(SOURCE['fetched_at']), None)
    answers = [SimpleNamespace(content=[{'type': 'tool_use', 'id': 'a', 'name': 'train_search', 'input': raw['query']},
                                        {'type': 'tool_use', 'id': 'b', 'name': 'weather_query', 'input': {'city': '北京'}}]),
               SimpleNamespace(text='{"summary":"本段火车已查询","execution":{"tool_calls":999},"query_results":[{"fake":true}]}')]
    async def model(messages, **kwargs):
        assert {s['function']['name'] for s in kwargs['tools']} == {'train_search', 'train_search_by_arrival'}
        return answers.pop(0)
    run = RunState('turn', workflow=w, current_task_id=t['id'])
    run.travel_conditions = deepcopy(w['confirmed_conditions'])
    result = await info_class()(model=model, tool_executor=ToolExecutor({'train_search': Provider()})).run(
        {'type': 'task_request', 'task': {'id': t['id'], 'revision': 1, 'requested_domains': ['train'], 'query_requests': []},
         'context': {'original_query': '火车酒店', 'effective_conditions': w['effective_conditions']}}, run)
    assert result['type'] == 'task_result' and result['task_revision'] == 1
    assert result['execution']['model_calls'] == 2
    assert result['execution']['tool_calls'] == 1
    assert result['query_results'][0]['items'][0]['price_cny'] == '100'
    assert 'fake' not in result['query_results'][0]
    assert run.travel_conditions == w['confirmed_conditions']
    assert set(next(iter(w['results_by_query'].values()))['parameters']) >= {'origin', 'departure_date'}


@pytest.mark.asyncio
async def test_executor_denies_foreign_city_before_provider_call():
    w = workflow(); t = w['tasks'][0]
    class Provider:
        async def search(self, query): raise AssertionError('must not call provider')
    run = RunState('turn', workflow=w, current_task_id=t['id'])
    result = await ToolExecutor({'train_search': Provider()}).execute('train_search',
        dict(origin='杭州', destination='上海', departure_date='2026-10-11'), run, call_id='bad')
    assert result['status'] == 'error'


def test_juhe_does_not_invent_arrival_day():
    from test_juhe_train_provider import search
    data, _ = search()
    item = data.items[0]
    assert item['departure_at'] == '2026-10-09T18:04:00+08:00'
    assert item['arrival_at'] is None


@pytest.mark.asyncio
async def test_tool_inherits_confirmed_constraints_in_query_record():
    w = workflow(); w['confirmed_conditions']['constraints'] = {'seat_class': '一等座'}
    t = w['tasks'][0]
    from workflow_runtime import OfflineProvider
    provider = OfflineProvider('train'); run = RunState('turn', workflow=w, current_task_id=t['id'])
    await ToolExecutor({'train_search': provider}).execute('train_search', train_result(t)['query'], run, call_id='t')
    row = next(iter(w['results_by_query'].values()))
    assert row['constraints'] == {'seat_class': '一等座'}
    assert query_views(w, t['id'])[0]['items'] == []


@pytest.mark.asyncio
async def test_current_window_reaches_main_without_a_new_provider_query():
    w = workflow(); t = w['tasks'][0]
    from workflow_runtime import OfflineProvider
    provider = OfflineProvider('train'); run = RunState('turn', workflow=w, current_task_id=t['id'])
    tool = ToolExecutor({'train_search': provider}); parameters = train_result(t)['query']
    await tool.execute('train_search', parameters, run, call_id='first')
    second = await tool.execute('train_search', {**parameters, 'candidate_offset': 5}, run, call_id='next')
    assert len(provider.calls) == 1
    assert query_views(w, t['id'])[0]['items'] == second['items']
    assert len(query_views(w, t['id'])[0]['items']) == 3


@pytest.mark.asyncio
async def test_proposed_hotel_dates_can_change_without_changing_confirmed_dates():
    from workflow_runtime import OfflineProvider
    w = workflow(); task = w['tasks'][0]
    provider = OfflineProvider('hotel'); tool = ToolExecutor({'hotel_search': provider})
    run = RunState('turn', workflow=w, current_task_id=task['id'])
    parameters = {'city': '北京', 'check_in': '2026-10-12', 'check_out': '2026-10-14', 'guests': 1}
    rejected = await tool.execute('hotel_search', parameters, run, call_id='fixed')
    assert rejected['status'] == 'error' and not provider.calls
    task['field_sources'].update(check_in='proposal', check_out='proposal')
    accepted = await tool.execute('hotel_search', parameters, run, call_id='proposal')
    assert accepted['status'] == 'ok' and len(provider.calls) == 1
    assert task['conditions']['check_in'] == '2026-10-11'
