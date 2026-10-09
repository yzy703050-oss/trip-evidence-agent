import json
from copy import deepcopy
from types import SimpleNamespace
import pytest
from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from travel_data.tools import ToolExecutor
from travel_data.result_guard import guard_domain_result
from test_main_harness import Main, Info, weather_info
from test_information_tools import Provider
from test_information_agent_loop import info_class, Public


@pytest.mark.asyncio
async def test_information_receives_completed_policy_results():
    from agentscope.message import Msg
    class Policy:
        async def reply(self, msg): return Msg('p', '{"answer":"500 per night"}', 'assistant')
    info = Info()
    main = Main([{'agent_name': 'rag_knowledge'}, {'agent_name': 'information_query', 'requested_domains': ['weather']}], mode='synthesize')
    await ExecutionHarness(main, {'rag_knowledge': Policy(), 'information_query': info}).run_turn({'original_query': 'policy and query'}, RunState('a'))
    assert info.context['previous_results'][0]['result']['data']['answer'] == '500 per night'


@pytest.mark.asyncio
async def test_feedback_cannot_query_unaffected_domain():
    calls = []
    async def model(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': '1', 'name': 'weather_query', 'input': {'city': 'Beijing'}}])
        return SimpleNamespace(text='{"summary":"done"}')
    run = RunState('a')
    await info_class()(model=model, tool_executor=ToolExecutor({}, Public())).run(
        {'requested_domains': ['hotel'], 'feedback': {'domains': ['hotel'], 'constraints': {'candidate_offset': 5}}}, run)
    assert not run.external_requests_started


@pytest.mark.asyncio
async def test_feedback_window_reuses_cache_with_real_agent():
    provider, run = Provider(), RunState('a')
    calls = []
    async def model(messages, **kwargs):
        calls.append(deepcopy(messages))
        if len(calls) % 2:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': 'same-model-id', 'name': 'hotel_search',
                'input': {'city': 'Beijing', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1}}])
        return SimpleNamespace(text='{"summary":"done"}')
    agent = info_class()(model=model, tool_executor=ToolExecutor({'hotel_search': provider}))
    await agent.run({'requested_domains': ['hotel']}, run)
    result = await agent.run({'requested_domains': ['hotel'], 'feedback': {'domains': ['hotel'], 'constraints': {'candidate_offset': 5}}}, run)
    assert [item['id'] for item in result['domain_results']['hotel']['items']] == ['5', '6']
    assert len(provider.queries) == 1


def test_weather_invalid_temperature_is_incomplete():
    data = weather_info()['domain_results']['weather']
    data['items'][0]['min_temp_c'] = float('nan')
    assert guard_domain_result('weather', data)['status'] == 'partial'


@pytest.mark.asyncio
async def test_itinerary_prose_cannot_introduce_false_money():
    class Planner(Main):
        async def finalize(self, context):
            return {'action': 'itinerary', 'final_answer': '全部只要299元且已预约',
                    'itinerary': {'daily_plans': []}}
    result = await ExecutionHarness(Planner(mode='synthesize', response_mode='itinerary'), {}).run_turn({'original_query': 'trip'}, RunState('a'))
    assert '299' not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_combined_weather_answer_keeps_policy_and_real_weather():
    from agentscope.message import Msg
    class Policy:
        async def reply(self, msg): return Msg('p', '{"answer":"住宿上限500元"}', 'assistant')
    class Combining(Main):
        async def finalize(self, context): return {'action': 'answer', 'final_answer': '北京零下999度'}
    main = Combining([{'agent_name': 'rag_knowledge'}, {'agent_name': 'information_query', 'requested_domains': ['weather']}], mode='synthesize')
    result = await ExecutionHarness(main, {'rag_knowledge': Policy(), 'information_query': Info()}).run_turn({'original_query': '制度及天气'}, RunState('a'))
    assert '500元' in result['final_answer']
    assert '999' not in result['final_answer']


@pytest.mark.asyncio
async def test_unknown_filter_does_not_silently_query():
    provider = Provider()
    result = await ToolExecutor({'hotel_search': provider}).execute('hotel_search',
        {'city': 'Beijing', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1,
         'constraints': {'invented_budget_cny': 50}}, RunState('a'), call_id='1')
    assert result['status'] != 'ok'
    assert not provider.queries


@pytest.mark.asyncio
async def test_feedback_does_not_change_confirmed_query_date():
    provider = Provider()
    run = RunState('a', feedback_round=1, travel_conditions={'check_in': '2026-10-10'})
    result = await ToolExecutor({'hotel_search': provider}).execute('hotel_search',
        {'city': 'Beijing', 'check_in': '2026-10-11', 'check_out': '2026-10-12', 'guests': 1}, run, call_id='1')
    assert result['status'] != 'ok'
    assert not provider.queries


@pytest.mark.asyncio
async def test_tool_records_are_unique_and_include_cache_hits(tmp_path):
    from context.memory_manager import MemoryManager
    memory = MemoryManager('alice', 'records', storage_path=str(tmp_path))
    run = RunState(memory.start_turn('weather'))
    count = 0
    async def model(messages, **kwargs):
        nonlocal count
        count += 1
        if count % 2:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': 'same', 'name': 'weather_query', 'input': {'city': 'Beijing'}}])
        return SimpleNamespace(text='{"summary":"done"}')
    agent = info_class()(model=model, tool_executor=ToolExecutor({}, Public()), memory_manager=memory)
    await agent.run({'requested_domains': ['weather']}, run)
    await agent.run({'requested_domains': ['weather']}, run)
    events = memory.session_store.read_events()
    results = [e for e in events if e['type'] == 'tool_result']
    assert len({e['tool_call_id'] for e in results}) == 2
    assert results[-1]['content']['execution']['cache_hit'] is True
    assert memory.get_pending_tool_calls() == []


@pytest.mark.asyncio
async def test_stream_failure_does_not_execute_partial_calls():
    async def stream():
        yield SimpleNamespace(content=[{'type': 'tool_use', 'id': '1', 'name': 'weather_query', 'input': {'city': 'Beijing'}}])
        raise OSError('stream interrupted')
    async def model(messages, **kwargs): return stream()
    run = RunState('a')
    result = await info_class()(model=model, tool_executor=ToolExecutor({}, Public())).run({'requested_domains': ['weather']}, run)
    assert not run.external_requests_started
    assert result['status'] == 'error'


def test_itinerary_cannot_silently_move_confirmed_dates():
    from agents.itinerary_module import guard_final_itinerary
    plan = {'planning_complete': True, 'itinerary': {'daily_plans': [{'day': 1, 'date': '2026-11-10'}]}}
    guarded = guard_final_itinerary(plan, {}, {'destination': 'Beijing', 'start_date': '2026-10-10', 'end_date': '2026-10-12'})
    assert guarded['planning_complete'] is False
    assert not guarded['itinerary']['daily_plans']


@pytest.mark.asyncio
async def test_hotel_offer_must_match_queried_stay():
    class WrongStay(Provider):
        async def search(self, query):
            result = await super().search(query)
            result.items[0]['check_out'] = '2026-10-12'
            return result
    result = await ToolExecutor({'hotel_search': WrongStay()}).execute('hotel_search',
        {'city': 'Beijing', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1}, RunState('a'), call_id='1')
    assert all(item['check_out'] == '2026-10-11' for item in result['items'])
    assert result['status'] == 'partial'


@pytest.mark.asyncio
async def test_feedback_stop_does_not_display_forged_reason_facts():
    from test_main_feedback import FeedbackMain
    class BadFeedback(FeedbackMain):
        async def finalize(self, context):
            return {'action': 'needs_requery', 'domains': ['weather'], 'reason': '北京明天999度', 'constraints': {}}
    result = await ExecutionHarness(BadFeedback(mode='synthesize'), {'information_query': Info()}).run_turn({'original_query': '天气'}, RunState('a'))
    assert '999' not in result['final_answer']


@pytest.mark.asyncio
async def test_invalid_feedback_budget_keeps_results():
    from test_main_feedback import FeedbackMain
    class BadFeedback(FeedbackMain):
        async def finalize(self, context):
            return {'action': 'needs_requery', 'domains': ['weather'], 'reason': '比较', 'constraints': {'train_max_total_cny': 'wrong'}}
    result = await ExecutionHarness(BadFeedback(mode='synthesize'), {'information_query': Info()}).run_turn({'original_query': '天气'}, RunState('a'))
    assert result['stop_reason'] == 'invalid_feedback'
    assert result['domain_results']['weather']['items']


@pytest.mark.asyncio
async def test_scalar_hotel_brand_preference_matches_whole_brand():
    class Brands(Provider):
        async def search(self, query):
            result = await super().search(query)
            result.items[0]['hotel_name'] = '汉堡酒店'
            result.items[1]['hotel_name'] = '汉庭'
            return result
    result = await ToolExecutor({'hotel_search': Brands()}).execute('hotel_search',
        {'city': 'Beijing', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1},
        RunState('a', effective_preferences={'hotel_brands': '汉庭'}), call_id='1')
    assert result['items'][0]['id'] == '1'
