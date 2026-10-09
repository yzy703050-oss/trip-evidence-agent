import json
from copy import deepcopy

import pytest
from agentscope.message import Msg
from agents.contracts import RunState
from agents.orchestration_agent import OrchestrationAgent


def weather_info():
    source = {'provider': 'weather', 'fetched_at': '2026-10-09T09:00:00+08:00', 'url': 'https://example.com/weather'}
    return {'status': 'ok', 'summary': '北京明天多云，18～25℃', 'missing_fields': [], 'travel_conditions': {},
        'domain_results': {'weather': {'status': 'ok', 'query': {'city': '北京', 'date': '2026-10-10'},
        'items': [{'date': '2026-10-10', 'description': '多云', 'min_temp_c': 18, 'max_temp_c': 25, 'source': source}],
        'source': source, 'missing_fields': [], 'fetched_at': source['fetched_at'], 'message': None}}}


class Main:
    def __init__(self, schedule=None, mode='forward', response_mode='answer'):
        self.schedule = schedule or [{'agent_name': 'information_query', 'priority': 2, 'requested_domains': ['weather']}]
        self.mode, self.response_mode = mode, response_mode
        self.finalize_calls = 0
        self.final_context = None

    async def plan(self, context):
        return {'response_mode': self.response_mode, 'finalization_mode': self.mode,
                'rewritten_query': context['original_query'], 'agent_schedule': self.schedule}

    async def finalize(self, context):
        self.finalize_calls += 1
        self.final_context = context
        return {'action': 'answer', 'final_answer': '综合回答'}


class Info:
    def __init__(self, value=None):
        self.value = value or weather_info()
        self.context = None
        self.executions = 0

    async def run(self, context, run):
        self.executions += 1
        self.context = deepcopy(context)
        run.domain_results.update(self.value['domain_results'])
        return deepcopy(self.value)


@pytest.mark.asyncio
async def test_complete_weather_skips_finalize():
    main, info = Main(), Info()
    result = await OrchestrationAgent(main_agent=main, agent_registry={'information_query': info}).run_turn(
        {'original_query': '北京明天天气'}, RunState('a'))
    assert result['finalization_method'] == 'forward'
    assert main.finalize_calls == 0
    assert '18' in result['final_answer']
    assert result['domain_results']['weather']['query']['date'] == '2026-10-10'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'status': 'partial'}, {'summary': ''},
    {'missing_fields': ['city']}, {'domain_results': {}}])
async def test_incomplete_result_requires_finalize(change):
    main = Main()
    info = Info({**weather_info(), **change})
    result = await OrchestrationAgent(main_agent=main, agent_registry={'information_query': info}).run_turn(
        {'original_query': '天气'}, RunState('a'))
    assert main.finalize_calls == 1
    assert result['finalization_method'] == 'synthesize'


@pytest.mark.asyncio
async def test_effective_preferences_precede_info():
    class Preference:
        async def reply(self, msg):
            return Msg('pref', json.dumps({'preferences': [{'type': 'hotel_brands', 'value': '汉庭', 'action': 'replace'}]}), 'assistant')
    main = Main([{'agent_name': 'information_query', 'priority': 1, 'requested_domains': ['weather']},
                 {'agent_name': 'preference', 'priority': 1, 'answer_role': 'context'}])
    info, run = Info(), RunState('a', effective_preferences={'hotel_brands': ['旧偏好']})
    await OrchestrationAgent(main_agent=main, agent_registry={'information_query': info, 'preference': Preference()}).run_turn(
        {'original_query': '以后住汉庭，北京明天天气'}, run)
    assert info.context['effective_preferences']['hotel_brands'] == '汉庭'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['forward', 'synthesize'])
@pytest.mark.parametrize('succeeds', [True, False])
async def test_weather_answer_confirms_only_successful_preference_changes(mode, succeeds):
    class Preference:
        async def reply(self, msg):
            value = {'preferences': [{'type': 'hotel_brands', 'value': '汉庭', 'action': 'replace'}]} if succeeds else {'status': 'error', 'error': 'failed'}
            return Msg('pref', json.dumps(value), 'assistant')
    main = Main([{'agent_name': 'preference', 'answer_role': 'context'},
                 {'agent_name': 'information_query', 'requested_domains': ['weather']}], mode=mode)
    result = await OrchestrationAgent(main_agent=main, agent_registry={'information_query': Info(), 'preference': Preference()}).run_turn(
        {'original_query': '以后酒店偏好汉庭，查北京天气'}, RunState('preference-confirm'))
    assert '18' in result['final_answer']
    assert ('汉庭' in result['final_answer']) is succeeds
    if succeeds:
        assert '偏好' in result['final_answer']


@pytest.mark.asyncio
async def test_rag_legacy_status_is_preserved():
    inputs = []
    class RAG:
        async def reply(self, msg):
            inputs.append(json.loads(msg.content))
            return Msg('rag', '{"status":"no_knowledge","answer":"没有找到"}', 'assistant')
    main = Main([{'agent_name': 'rag_knowledge', 'priority': 1}], mode='synthesize')
    result = await OrchestrationAgent(main_agent=main, agent_registry={'rag_knowledge': RAG()}).run_turn(
        {'original_query': '报销标准'}, RunState('a'))
    assert inputs[0]['context']['rewritten_query'] == '报销标准'
    assert 'previous_results' in inputs[0]
    assert result['results'][0]['status'] == 'no_knowledge'
    assert main.finalize_calls == 1


@pytest.mark.asyncio
async def test_pending_answer_requires_synthesis():
    class Memory:
        async def reply(self, msg):
            return Msg('memory', '{"answer":"历史资料"}', 'assistant')
    main = Main([{'agent_name': 'memory_query', 'priority': 1},
                 {'agent_name': 'information_query', 'priority': 2, 'requested_domains': ['weather']}])
    await OrchestrationAgent(main_agent=main, agent_registry={'memory_query': Memory(), 'information_query': Info()}).run_turn(
        {'original_query': '我的历史和明天天气'}, RunState('a'))
    assert main.finalize_calls == 1


@pytest.mark.asyncio
async def test_multiple_complete_tool_domains_can_forward():
    value = weather_info()
    source = value['domain_results']['weather']['source']
    value['domain_results']['web'] = {'status': 'ok', 'query': {'query': '北京天气说明'},
        'items': [{'title': '预报说明', 'snippet': '天气资料', 'url': 'https://example.com/forecast', 'source': source}],
        'source': source, 'fetched_at': source['fetched_at'], 'missing_fields': [], 'message': None}
    main = Main([{'agent_name': 'information_query', 'requested_domains': ['weather', 'web']}])
    result = await OrchestrationAgent(main_agent=main, agent_registry={'information_query': Info(value)}).run_turn(
        {'original_query': '北京天气及说明'}, RunState('a'))
    assert result['finalization_method'] == 'forward'
    assert main.finalize_calls == 0
