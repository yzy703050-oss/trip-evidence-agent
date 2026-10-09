import importlib.util
from pathlib import Path
from types import SimpleNamespace
import json

import pytest

from agents.contracts import RunState
from travel_data.contracts import AgentDataResult, Source
from datetime import datetime, timezone
from travel_data.tools import ToolExecutor


def info_class():
    path = Path('.claude/skills/query-info/script/agent.py')
    spec = importlib.util.spec_from_file_location('information_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.InformationQueryAgent


class Public:
    async def weather(self, city, requested_date):
        source = Source('weather', datetime(2026, 10, 9, tzinfo=timezone.utc), 'https://example.com/weather')
        return AgentDataResult('ok', {'city': city, 'date': requested_date},
            [{'date': requested_date, 'description': '多云', 'min_temp_c': 18, 'max_temp_c': 25,
              'source': source.to_dict()}], [], source, source.fetched_at, None)


@pytest.mark.asyncio
async def test_multiple_tool_results_reach_same_agent():
    calls = []
    async def model(messages, **kwargs):
        calls.append(list(messages))
        if len(calls) == 1:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': 'weather-1', 'name': 'weather_query',
                'input': {'city': '北京', 'date': '2026-10-10'}},
                {'type': 'tool_use', 'id': 'bad-1', 'name': 'unknown', 'input': {}}])
        return SimpleNamespace(text=json.dumps({'summary': '天气已取得', 'domain_results': {'weather': {'items': [{'fake': True}]}}}))
    info = await info_class()(model=model, tool_executor=ToolExecutor({}, Public())).run(
        {'original_query': '北京明天天气', 'requested_domains': ['weather']}, RunState('a'))
    assert info['domain_results']['weather']['items'][0]['min_temp_c'] == 18
    assert info['summary'] == '天气已取得'
    assert {m['tool_call_id'] for m in calls[1] if m['role'] == 'tool'} == {'weather-1', 'bad-1'}
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_info_model_and_tool_limits():
    calls = []
    async def model(messages, **kwargs):
        calls.append(1)
        return SimpleNamespace(content=[{'type': 'tool_use', 'id': str(len(calls)),
            'name': 'weather_query', 'input': {'city': '北京', 'date': '2026-10-10'}}])
    run = RunState('a')
    result = await info_class()(model=model, tool_executor=ToolExecutor({}, Public())).run(
        {'requested_domains': ['weather']}, run)
    assert len(calls) == 6
    assert len(run.tool_requests) == 6
    assert result['status'] == 'partial'
    assert result['domain_results']['weather']['items']


@pytest.mark.asyncio
async def test_ten_tool_limit_returns_paired_overflow_result():
    seen = []
    async def model(messages, **kwargs):
        seen.append(list(messages))
        if len(seen) == 1:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': str(i), 'name': 'weather_query',
                'input': {'city': '北京', 'date': '2026-10-10'}} for i in range(11)])
        return SimpleNamespace(text='{"summary":"天气"}')
    run = RunState('a')
    await info_class()(model=model, tool_executor=ToolExecutor({}, Public())).run({'requested_domains': ['weather']}, run)
    assert len(run.tool_requests) == 10
    messages = [m for m in seen[1] if m['role'] == 'tool']
    assert len(messages) == 11
    assert json.loads(messages[-1]['content'])['status'] == 'error'
