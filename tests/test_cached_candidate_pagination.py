"""Local candidate browsing must preserve evidence without supplier requests."""

from copy import deepcopy
from datetime import datetime

import pytest
from test_workflow_queries import SOURCE, train_result
from workflow_support import workflow

from agents.context_projection import project_domain_result
from agents.contracts import RunState
from agents.workflow_batches import initialize_query_containers
from agents.workflow_queries import query_views, resolve_selection
from travel_data.batch_execution import execute_query_batch
from travel_data.contracts import AgentDataResult, Source
from travel_data.tools import ToolExecutor


def seed_cached_train(count=12):
    w = workflow()
    initialize_query_containers(w)
    run = RunState('pages', workflow=w)

    class Provider:
        calls = 0

        async def search(self, query):
            self.calls += 1
            task = {'origin': query.origin, 'destination': query.destination,
                    'conditions': query.to_dict()}
            data = train_result(task, count=count)
            source = Source('offline', datetime.fromisoformat(SOURCE['fetched_at']), SOURCE['url'])
            return AgentDataResult('ok', query.to_dict(), data['items'], [],
                                   source, source.fetched_at, None)

    executor = ToolExecutor({'train_search': Provider()})
    return w, run, executor


def batch_for(w, offset=0, **extra):
    task = w['tasks'][0]
    return {'requests': [{
        'request_id': f'page-{offset}', 'domain': 'train', 'name': 'train_search',
        'task_id': task['id'], 'task_revision': task['revision'],
        'parameters': {**train_result(task)['query'], 'candidate_offset': offset},
        **extra,
    }]}


@pytest.mark.asyncio
async def test_batch_pagination_keeps_real_window_and_query_identity():
    w, run, executor = seed_cached_train()
    pages = []
    for offset in (0, 5, 10):
        result = await execute_query_batch(batch_for(w, offset), run, executor)
        page = result['results'][0]
        pages.append(page)
        row = query_views(w, w['tasks'][0]['id'])[0]
        assert row['view_offset'] == offset
        assert [i['id'] for i in row['items']] == [i['id'] for i in page['items']]
    assert [len(p['items']) for p in pages] == [5, 5, 2]
    assert [p['view_offset'] for p in pages] == [0, 5, 10]
    assert [p['has_more'] for p in pages] == [True, True, False]
    assert len({p['query_id'] for p in pages}) == 1
    assert len({p['result_revision'] for p in pages}) == 1
    assert len({p['fetched_at'] for p in pages}) == 1
    assert run.external_request_count == 1
    assert executor.providers['train_search'].calls == 1
    row = w['results_by_query'][pages[0]['query_id']]
    assert len(row['items']) == 12
    assert resolve_selection(w, row['task_id'], row['task_revision'], {
        'query_id': row['id'], 'result_revision': row['result_revision'],
        'candidate_id': pages[0]['items'][0]['id'],
    })['id'] == pages[0]['items'][0]['id']


def test_model_projection_preserves_page_without_slicing_twice():
    data = train_result(workflow()['tasks'][0], count=12)
    row = {**data, 'domain': 'train', 'items': data['items'][10:],
           'candidate_total': 12, 'view_offset': 10}
    before = deepcopy(row)
    view = project_domain_result(row)
    assert view['view_offset'] == 10
    assert len(view['items']) == 2 and view['has_more'] is False
    assert view['items'][0]['id'] == data['items'][10]['id']
    assert row == before


@pytest.mark.asyncio
async def test_query_views_explicit_window_has_correct_offset():
    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    row = query_views(w, w['tasks'][0]['id'], offset=10)[0]
    assert row['view_offset'] == 10 and len(row['items']) == 2


@pytest.mark.asyncio
async def test_cached_page_runs_with_zero_external_budget():
    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    run.workflow_tool_budget = 0
    run.external_requests_by_task[w['tasks'][0]['id']] = 10
    result = await execute_query_batch(batch_for(w, 5, cache_only=True), run, executor)
    assert not result['gaps']
    assert len(result['results'][0]['items']) == 5
    assert result['results'][0]['view_offset'] == 5
    assert run.external_request_count == 1 and run.workflow_tool_budget == 0
    assert executor.providers['train_search'].calls == 1


@pytest.mark.asyncio
async def test_cache_disappears_before_read_never_calls_provider():
    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    run.candidates.cache.clear()
    before = deepcopy(w['results_by_query'])
    run.current_task_id = w['tasks'][0]['id']
    result = await executor.execute('train_search', batch_for(w, 5)['requests'][0]['parameters'],
                                    run, call_id='missing', cache_only=True)
    assert result['status'] == 'error'
    assert w['results_by_query'] == before
    assert executor.providers['train_search'].calls == 1


@pytest.mark.asyncio
async def test_failed_cached_batch_preserves_current_query_container():
    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    run.candidates.cache.clear()
    before = deepcopy(w['tasks'][0]['query_containers'])
    evidence = deepcopy(w['results_by_query'])
    result = await execute_query_batch(batch_for(w, 5, cache_only=True), run, executor)
    assert result['results'][0]['status'] == 'error'
    assert w['tasks'][0]['query_containers'] == before
    assert w['results_by_query'] == evidence
    assert executor.providers['train_search'].calls == 1


@pytest.mark.asyncio
async def test_hotel_shared_cache_does_not_authorize_changed_stay():
    from agents.cached_pagination import cached_page_options, validate_cached_page_batch
    from agents.workflow_queries import as_result
    from travel_data.candidates import CandidateStore, query_cache_key
    from workflow_support import populated_workflow

    w = populated_workflow()
    task = w['tasks'][0]
    row = next(r for r in w['results_by_query'].values()
               if r['domain'] == 'hotel' and r['task_id'] == task['id'])
    row['parameters'].update(check_in=task['conditions']['check_in'],
                             check_out=task['conditions']['check_out'], guests=1)
    row['items'] = [{**row['items'][0], 'id': f'hotel-{i}'} for i in range(12)]
    candidates = CandidateStore()
    key = query_cache_key('hotel', row['parameters'])
    candidates.cache[key] = as_result(row)
    option = cached_page_options(w, candidates, limit=5)[0]
    assert option['domain'] == 'hotel'
    changed = deepcopy(option)
    changed['parameters']['check_out'] = '2026-10-20'
    assert query_cache_key('hotel', {**row['parameters'], 'check_out': '2026-10-20'}) == key
    with pytest.raises(ValueError):
        validate_cached_page_batch({'requests': [changed]}, w, candidates, limit=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['refresh', 'date', 'offset', 'constraints', 'duplicate',
                                  'missing', 'failed', 'stale', 'boolean', 'backwards'])
async def test_invalid_cached_batch_is_atomic(change):
    from agents.cached_pagination import validate_cached_page_batch

    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    batch = batch_for(w, 5)
    request = batch['requests'][0]
    if change == 'refresh':
        request['parameters']['refresh'] = True
    elif change == 'date':
        request['parameters']['departure_date'] = '2026-10-12'
    elif change == 'offset':
        request['parameters']['candidate_offset'] = 500
    elif change == 'boolean':
        request['parameters']['candidate_offset'] = True
    elif change == 'backwards':
        request['parameters']['candidate_offset'] = 0
    elif change == 'constraints':
        request['parameters']['constraints'] = {'seat_class': '一等座'}
    elif change == 'duplicate':
        batch['requests'].append({**deepcopy(request), 'request_id': 'duplicate'})
    elif change == 'missing':
        run.candidates.cache.clear()
    elif change == 'failed':
        from dataclasses import replace
        key = next(iter(run.candidates.cache))
        run.candidates.cache[key] = replace(run.candidates.cache[key], status='error')
    else:
        w['tasks'][0]['revision'] += 1
    before = deepcopy(w)
    with pytest.raises(ValueError):
        validate_cached_page_batch(batch, w, run.candidates, limit=5)
    assert w == before and executor.providers['train_search'].calls == 1


@pytest.mark.asyncio
async def test_options_and_validation_preserve_query_identity():
    from agents.cached_pagination import cached_page_options, validate_cached_page_batch

    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    before = deepcopy(w)
    options = cached_page_options(w, run.candidates, limit=5)
    assert len(options) == 1
    assert options[0]['parameters']['candidate_offset'] == 5
    assert options[0]['query_id'] in w['results_by_query']
    batch = batch_for(w, 5)
    checked = validate_cached_page_batch(batch, w, run.candidates, limit=5)
    assert checked['requests'][0]['cache_only'] is True
    assert 'cache_only' not in batch['requests'][0]
    assert w == before


@pytest.mark.asyncio
async def test_hotel_update_cannot_page_train_and_stale_results_not_offered():
    from agents.cached_pagination import cached_page_options

    w, run, executor = seed_cached_train()
    await execute_query_batch(batch_for(w), run, executor)
    task = w['tasks'][0]
    task['update_scope'] = ['hotel']
    assert cached_page_options(w, run.candidates, limit=5) == []
    task.pop('update_scope')
    task['revision'] += 1
    assert cached_page_options(w, run.candidates, limit=5) == []


async def run_paging_main(tmp_path, *, repeat=False, decision_limit=None, first_page_only=False,
                          target_domain='train'):
    from test_consolidated_travel_run import BatchMain
    from workflow_runtime import OfflineProvider

    from agents.execution_harness import ExecutionHarness
    from agents.workflow_runner import WorkflowLimits
    from context.memory_manager import MemoryManager

    class Main(BatchMain):
        request = None
        first_id = None

        async def select_batch(self, context):
            if self.request is None:
                assert context['query_scope'] == 'cached_pages_only'
                assert context['allowed_actions'] == ['commit_batch', 'schedule_queries']
                option = next(o for o in context['cached_page_options'] if o['domain'] == target_domain)
                self.request = {k: deepcopy(v) for k, v in option.items()
                                if k not in {'query_id', 'result_revision'}}
                first = next(r for r in context['task_results'] if r.get('id') == option['query_id'])
                self.first_id = first['items'][0]['id']
                self.calls += 1
                return {'action': 'schedule_queries', 'query_plan': [deepcopy(self.request)]}
            if repeat:
                self.calls += 1
                return {'action': 'schedule_queries', 'query_plan': [deepcopy(self.request)]}
            row = next(r for r in context['task_results']
                       if r.get('task_id') == self.request['task_id'] and r['domain'] == self.request['domain'])
            assert row['view_offset'] == 5 and row['items'][0]['id'] != self.first_id
            action = await super().select_batch(context)
            if first_page_only:
                action['draft_tasks'][0]['draft_plan']['train_selection']['candidate_id'] = self.first_id
            return action

    main = Main()
    class Hotel(OfflineProvider):
        async def search(self, query):
            from dataclasses import replace
            data = await super().search(query)
            if target_domain == 'hotel':
                data = replace(data, items=[{**data.items[0], 'id': f'place-{i}'} for i in range(12)])
            return data

    train, hotel = OfflineProvider('train'), Hotel('hotel')
    memory = MemoryManager('pages', 'integration', str(tmp_path))
    harness = ExecutionHarness(main, {}, memory)
    harness.runner.executor = ToolExecutor({'train_search': train, 'hotel_search': hotel})
    if decision_limit:
        harness.runner.limits = WorkflowLimits(main_decisions_per_turn=decision_limit)
    run = RunState('integration')
    result = await harness.run_turn(
        {'original_query': '安排往返', 'current_time': '2026-10-10T10:00:00+08:00'}, run)
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    assert run.external_request_count == 3
    return result, main


@pytest.mark.asyncio
@pytest.mark.parametrize('first_page_only', [False, True])
async def test_main_reads_cached_page_before_commit(tmp_path, first_page_only):
    result, main = await run_paging_main(tmp_path, first_page_only=first_page_only)
    assert result['status'] == 'completed', result
    assert result['workflow']['validation']['valid']
    assert main.calls == 3


@pytest.mark.asyncio
async def test_main_pages_hotel_places_without_supplier_call(tmp_path):
    result, main = await run_paging_main(tmp_path, target_domain='hotel')
    assert result['status'] == 'completed', result
    selection = result['workflow']['tasks'][0]['draft_plan']['hotel_selection']
    assert selection['candidate_id'] == 'place-5'
    assert main.calls == 3


@pytest.mark.asyncio
async def test_repeated_page_action_stops_without_refetch(tmp_path):
    result, _ = await run_paging_main(tmp_path, repeat=True)
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'no_progress'


@pytest.mark.asyncio
async def test_pagination_stops_at_decision_budget(tmp_path):
    result, _ = await run_paging_main(tmp_path, repeat=True, decision_limit=1)
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'main_decision_limit'
    assert '缓存' in result['final_answer'] and '未' in result['final_answer']


@pytest.mark.asyncio
async def test_no_more_pages_keeps_commit_only(tmp_path):
    from test_consolidated_travel_run import BatchMain
    from workflow_runtime import OfflineProvider
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager

    class Provider(OfflineProvider):
        async def search(self, query):
            from dataclasses import replace
            data = await super().search(query)
            return replace(data, items=data.items[:5])

    class Main(BatchMain):
        async def select_batch(self, context):
            assert context['allowed_actions'] == ['commit_batch']
            return await super().select_batch(context)

    main = Main()
    harness = ExecutionHarness(main, {}, MemoryManager('pages', 'short', str(tmp_path)))
    harness.runner.executor = ToolExecutor({'train_search': Provider('train'),
                                           'hotel_search': Provider('hotel')})
    result = await harness.run_turn(
        {'original_query': '安排往返', 'current_time': '2026-10-10T10:00:00+08:00'}, RunState('short'))
    assert result['status'] == 'completed' and main.calls == 2


def test_cached_page_prompt_explains_scope():
    from agents.travel_prompts import selection_prompt

    prompt = selection_prompt({'allowed_actions': ['commit_batch', 'schedule_queries'],
                               'query_scope': 'cached_pages_only'})
    assert '本地缓存翻页' in prompt and 'cached_page_options' in prompt
    assert '不必遍历' in prompt
