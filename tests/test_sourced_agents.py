import asyncio
import json
from datetime import datetime, timezone

import pytest
from agentscope.message import Msg
from agents.lazy_agent_registry import LazyAgentRegistry
from travel_data.contracts import AgentDataResult, Source


CASES = [
    ('train_search', {'origin': '上海', 'destination': '北京', 'departure_date': '2026-10-20'}, 'departure_date'),
    ('hotel_search', {'city': '北京', 'check_in': '2026-10-20', 'check_out': '2026-10-22', 'guests': 2}, 'check_in'),
    ('travel_guide', {'destination': '北京', 'visit_dates': ['2026-10-20']}, 'destination'),
]


class RecordingProvider:
    def __init__(self, fail=False, empty=False):
        self.queries = []
        self.fail = fail
        self.empty = empty
        self.source = Source('test-only', datetime(2026, 10, 8, tzinfo=timezone.utc), 'https://example.org')

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise TimeoutError('test timeout')
        return AgentDataResult('ok', query.to_dict(), [] if self.empty else [{'source': self.source.to_dict()}], [], self.source, self.source.fetched_at, None)


def invoke(name, fields, provider=None, previous=False):
    registry = LazyAgentRegistry(None, {}, providers={name: provider} if provider else {})
    payload = {'context': fields, 'previous_results': []}
    if previous:
        payload = {'context': {'user_preferences': {'origin': '杭州'}}, 'previous_results': [{'agent_name': 'event_collection', 'status': 'success', 'data': fields}]}
    msg = Msg('user', json.dumps(payload), 'user')
    return json.loads(asyncio.run(registry[name].reply(msg)).content)


@pytest.mark.parametrize('name,fields,required', CASES)
@pytest.mark.parametrize('previous', [False, True])
def test_queries_and_sources_pass_through(name, fields, required, previous):
    provider = RecordingProvider()
    result = invoke(name, fields, provider, previous)
    assert result['source'] == provider.source.to_dict()
    assert result['items'] == [{'source': provider.source.to_dict()}]
    assert provider.queries[0].to_dict() == {**fields, **({'passengers': 1} if name == 'train_search' else {})}


@pytest.mark.parametrize('name,fields,required', CASES)
def test_missing_does_not_query(name, fields, required):
    provider = RecordingProvider()
    result = invoke(name, {k: v for k, v in fields.items() if k != required}, provider)
    assert result['status'] == 'needs_input'
    assert required in result['missing_fields']
    assert not provider.queries


@pytest.mark.parametrize('name,fields,required', CASES)
def test_unavailable_timeout_and_empty(name, fields, required):
    result = invoke(name, fields)
    assert result['status'] == 'unavailable'
    assert result['source'] is None and result['fetched_at'] is None
    assert invoke(name, fields, RecordingProvider(fail=True))['status'] == 'error'
    empty = invoke(name, fields, RecordingProvider(empty=True))
    assert empty['status'] == 'ok' and empty['items'] == []


def test_invalid_date_and_guest_count_do_not_query():
    for overrides in [{'check_out': '2026-10-19'}, {'guests': 0}, {'check_in': 'tomorrow'}]:
        provider = RecordingProvider()
        assert invoke('hotel_search', {**CASES[1][1], **overrides}, provider)['status'] == 'needs_input'
        assert not provider.queries


def test_guide_without_dates_and_profile_not_used():
    provider = RecordingProvider()
    assert invoke('travel_guide', {'destination': '北京'}, provider)['query']['visit_dates'] == []
    assert invoke('train_search', {'user_preferences': CASES[0][1]}, provider)['status'] == 'needs_input'


def test_collector_aliases_and_failed_collector_are_not_trusted():
    provider = RecordingProvider()
    fields = {**CASES[0][1]}
    fields['start_date'] = fields.pop('departure_date')
    assert invoke('train_search', fields, provider, previous=True)['status'] == 'ok'
    registry = LazyAgentRegistry(None, {}, providers={'train_search': provider})
    payload = {'previous_results': [{'agent_name': 'event_collection', 'status': 'error', 'data': fields}]}
    result = json.loads(asyncio.run(registry['train_search'].reply(Msg('user', json.dumps(payload), 'user'))).content)
    assert result['status'] == 'needs_input'
    assert len(provider.queries) == 1


def test_provider_failure_does_not_expose_exception_details():
    class FailingProvider:
        async def search(self, query):
            raise RuntimeError('secret-token')
    result = invoke('travel_guide', {'destination': '北京'}, FailingProvider())
    assert result['status'] == 'error'
    assert 'secret-token' not in json.dumps(result)
