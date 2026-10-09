import asyncio
import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import agents.orchestration_agent as routing
from agents.intention_agent import IntentionAgent
from agentscope.message import Msg


def schedule(*names):
    return [{'agent_name': name, 'priority': i + 1} for i, name in enumerate(names)]


def test_dependencies_parallel_and_deduplication():
    rows = routing.normalize_schedule(schedule('itinerary_planning', 'train_search', 'hotel_search', 'event_collection', 'train_search', 'travel_guide'))
    priorities = {r['agent_name']: r['priority'] for r in rows}
    assert len(rows) == 5
    assert priorities['event_collection'] < priorities['train_search'] == priorities['hotel_search'] == priorities['travel_guide'] < priorities['itinerary_planning']


def test_train_only_inserts_collection_without_planning():
    rows = routing.normalize_schedule(schedule('train_search'))
    assert [r['agent_name'] for r in rows] == ['event_collection', 'train_search']


def test_weather_and_legacy_order_are_preserved():
    original = schedule('memory_query', 'rag_knowledge', 'information_query', 'preference')
    assert routing.normalize_schedule(original) == original


def test_orchestrator_delivers_completed_dependencies_and_isolates_failure():
    calls = {}
    class Stub:
        def __init__(self, name): self.name = name
        async def reply(self, msg):
            calls[self.name] = json.loads(msg.content)['previous_results']
            if self.name == 'train_search': raise TimeoutError('test failure')
            return Msg('stub', json.dumps({'destination': '北京'}), 'assistant')
    names = ['itinerary_planning', 'hotel_search', 'train_search']
    orchestrator = routing.OrchestrationAgent(agent_registry={n: Stub(n) for n in names + ['event_collection']})
    result = asyncio.run(orchestrator.reply(Msg('user', json.dumps({'agent_schedule': schedule(*names)}), 'user')))
    assert [r['agent_name'] for r in calls['hotel_search']] == ['event_collection']
    assert {r['agent_name'] for r in calls['itinerary_planning']} == {'event_collection', 'hotel_search', 'train_search'}
    assert json.loads(result.content)['status'] == 'partial_failure'


def event_agent(model):
    path = Path('.claude/skills/event-collection/script/agent.py')
    spec = importlib.util.spec_from_file_location('event_collection_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.EventCollectionAgent(model=model)


def test_event_conditions_missing_stay_missing():
    captured = []
    async def model(messages):
        captured.append(messages[0]['content'])
        return SimpleNamespace(text=json.dumps({'destination': '北京', 'missing_info': []}))
    result = asyncio.run(event_agent(model).reply(Msg('user', json.dumps({'context': {'rewritten_query': '北京住两晚', 'user_preferences': {'home_location': '上海'}}}), 'user')))
    data = json.loads(result.content)
    for key in ['guests', 'check_in', 'check_out']:
        assert data[key] is None
        assert key in data['missing_info']
    assert '不能' in captured[0] and '家庭住址' in captured[0]


def test_event_returns_explicit_hotel_conditions():
    async def model(messages):
        assert all(key in messages[0]['content'] for key in ['guests', 'check_in', 'check_out'])
        return SimpleNamespace(text=json.dumps({'guests': 2, 'check_in': '2026-10-20', 'check_out': '2026-10-22'}))
    data = json.loads(asyncio.run(event_agent(model).reply(Msg('user', '两人北京10月20日入住，22日离店', 'user'))).content)
    assert (data['guests'], data['check_in'], data['check_out']) == (2, '2026-10-20', '2026-10-22')


def test_intention_prompt_exposes_domains_and_query_only_boundary():
    prompts = []
    async def model(messages):
        prompts.append(messages[-1]['content'])
        return SimpleNamespace(text='{}')
    asyncio.run(IntentionAgent(model=model).reply(Msg('user', '明天天气', 'user')))
    prompt = prompts[0]
    assert all(name in prompt for name in ['train_search', 'hotel_search', 'travel_guide'])
    assert '无需' in prompt and '普通天气' in prompt


def test_rendered_intention_prompt_separates_city_information_from_attractions():
    prompts = []
    async def model(messages):
        prompts.append(messages[-1]['content'])
        return SimpleNamespace(text='{}')
    asyncio.run(IntentionAgent(model=model).reply(Msg('user', '北京有什么好玩的？', 'user')))
    lines = prompts[0].splitlines()
    city = next(line for line in lines if '"北京怎么样？"' in line)
    attractions = next(line for line in lines if '"北京有什么好玩的？"' in line)
    assert 'information_query' in city and 'travel_guide' not in city
    assert 'travel_guide' in attractions and 'information_query' not in attractions
