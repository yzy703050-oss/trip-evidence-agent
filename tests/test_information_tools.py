import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from agents.contracts import RunState
from travel_data.contracts import AgentDataResult, HotelOffer, Source


class Provider:
    def __init__(self):
        self.queries = []

    async def search(self, query):
        self.queries.append(query)
        await asyncio.sleep(0)
        source = Source('test', datetime(2026, 10, 9, tzinfo=timezone.utc), 'https://example.com')
        items = [HotelOffer(str(i), f'酒店{i}', '大床房', query.check_in, query.check_out,
                 query.guests, Decimal(str(100+i)), True, 'available', None, source, None).to_dict()
                 for i in range(7)]
        return AgentDataResult('ok', query.to_dict(), items, [], source, source.fetched_at, None)


@pytest.mark.asyncio
async def test_missing_guests_does_not_request_provider():
    from travel_data.tools import ToolExecutor
    provider, run = Provider(), RunState('a')
    result = await ToolExecutor({'hotel_search': provider}).execute('hotel_search',
            {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11'}, run, call_id='a')
    assert result['status'] == 'needs_input'
    assert 'guests' in result['missing_fields']
    assert provider.queries == []
    assert not run.external_requests_started


@pytest.mark.asyncio
async def test_candidate_window_does_not_refetch():
    from travel_data.tools import ToolExecutor
    provider, run = Provider(), RunState('a')
    executor = ToolExecutor({'hotel_search': provider})
    query = {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1}
    first = await executor.execute('hotel_search', query, run, call_id='a')
    second = await executor.execute('hotel_search', {**query, 'candidate_offset': 5}, run, call_id='b')
    assert len(first['items']) == 5
    assert len(second['items']) == 2
    assert len(provider.queries) == 1
    assert run.tool_requests[-1]['cache_hit'] is True


@pytest.mark.asyncio
async def test_concurrent_same_query_fetches_once():
    from travel_data.tools import ToolExecutor
    provider, run = Provider(), RunState('a')
    executor = ToolExecutor({'hotel_search': provider})
    args = {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1}
    await asyncio.gather(executor.execute('hotel_search', args, run, call_id='a'),
                         executor.execute('hotel_search', args, run, call_id='b'))
    assert len(provider.queries) == 1


@pytest.mark.asyncio
async def test_decimal_budget_filters_before_window():
    from travel_data.tools import ToolExecutor
    result = await ToolExecutor({'hotel_search': Provider()}).execute('hotel_search',
        {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1,
         'constraints': {'hotel_max_total_cny': '100.01'}}, RunState('a'), call_id='a')
    assert [item['id'] for item in result['items']] == ['0']


@pytest.mark.asyncio
async def test_weather_date_outside_response_is_incomplete():
    from travel_data.public_query import PublicQueryProvider
    result = await PublicQueryProvider(weather_fetch=lambda city: {
        'current_condition': [{'temp_C': '20'}],
        'weather': [{'date': '2026-10-10', 'mintempC': '18', 'maxtempC': '25'}]
    }).weather('北京', '2026-11-10')
    assert result.status == 'partial'
    assert result.items == []


@pytest.mark.asyncio
async def test_unknown_tool_does_not_request_provider():
    from travel_data.tools import ToolExecutor
    run = RunState('a')
    assert (await ToolExecutor({}).execute('shell', {}, run, call_id='a'))['status'] == 'error'
    assert not run.external_requests_started


@pytest.mark.asyncio
async def test_failed_parameter_attempt_is_recorded():
    from travel_data.tools import ToolExecutor
    run = RunState('a')
    await ToolExecutor({}).execute('hotel_search', {}, run, call_id='missing')
    assert run.tool_requests[0]['id'] == 'missing'
    assert run.tool_requests[0]['status'] == 'needs_input'


@pytest.mark.asyncio
@pytest.mark.parametrize('amount', ['NaN', 'Infinity', '-1', 'wrong'])
async def test_invalid_budget_never_requests_provider(amount):
    from travel_data.tools import ToolExecutor
    provider, run = Provider(), RunState('a')
    result = await ToolExecutor({'hotel_search': provider}).execute('hotel_search',
        {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1,
         'constraints': {'hotel_max_total_cny': amount}}, run, call_id='a')
    assert result['status'] == 'needs_input'
    assert provider.queries == []
