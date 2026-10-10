from copy import deepcopy
from datetime import date

import pytest


@pytest.mark.asyncio
async def test_arrival_search_finds_previous_day_departure_and_preserves_evidence():
    from travel_data.arrival_search import search_by_arrival
    from evals.simulated_travel import SimulatedTrainProvider
    provider = SimulatedTrainProvider(duration_minutes=600, departure_hours=[20])
    result = await search_by_arrival(provider, {'origin': '重庆', 'destination': '上海',
                                               'arrival_date': '2026-10-18', 'passengers': 2}, request_budget=3)
    assert result.status == 'ok'
    assert provider.calls == [date(2026, 10, 18), date(2026, 10, 17)]
    assert result.items[0]['departure_at'].startswith('2026-10-17T20:00')
    assert result.items[0]['arrival_at'].startswith('2026-10-18T06:00')
    assert result.items[0]['source']['provider'].startswith('simulation:')


@pytest.mark.asyncio
async def test_arrival_deadline_filters_same_day_late_arrivals():
    from travel_data.arrival_search import search_by_arrival
    from evals.simulated_travel import SimulatedTrainProvider
    provider = SimulatedTrainProvider(duration_minutes=120, departure_hours=[8, 14])
    result = await search_by_arrival(provider, {'origin': '北京', 'destination': '上海',
        'arrival_date': '2026-10-18', 'arrival_before': '2026-10-18T12:00:00+08:00'}, request_budget=3)
    assert result.items and all(i['arrival_time'] == '10:00' for i in result.items)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_arrival_search_obeys_external_request_budget():
    from travel_data.arrival_search import search_by_arrival
    from evals.simulated_travel import SimulatedTrainProvider
    provider = SimulatedTrainProvider(duration_minutes=1800, departure_hours=[20])
    result = await search_by_arrival(provider, {'origin': '重庆', 'destination': '上海',
        'arrival_date': '2026-10-18'}, request_budget=1)
    assert len(provider.calls) == 1
    assert result.status == 'partial'
    assert result.items == []


@pytest.mark.asyncio
async def test_unavailable_provider_is_not_repeated_for_other_dates():
    from travel_data.arrival_search import search_by_arrival
    from travel_data.contracts import AgentDataResult
    class Provider:
        calls = 0
        async def search(self, query):
            self.calls += 1
            return AgentDataResult('unavailable', query.to_dict(), [], [], None, None, '配额耗尽')
    provider = Provider()
    result = await search_by_arrival(provider, {'origin': '重庆', 'destination': '上海',
        'arrival_date': '2026-10-18'}, request_budget=3)
    assert provider.calls == 1
    assert result.status == 'unavailable'
    assert result.items == []


def test_duration_adapter_calculates_cross_year_and_more_than_24_hours():
    from test_juhe_train_provider import ROW, search, NOW
    row = deepcopy(ROW)
    row.update(departure_time='23:00', arrival_time='01:30', duration='26:30')
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 12, 31, 9, tzinfo=timezone(timedelta(hours=8)))
    result, _ = search(row=row, day=now.date(), now_fn=lambda: now)
    assert result.items[0]['arrival_at'] == '2027-01-02T01:30:00+08:00'
    assert result.items[0]['time_evidence']['arrival'] == 'provider_segment_duration'


def test_inconsistent_duration_does_not_create_false_arrival_date():
    from test_juhe_train_provider import ROW, search
    row = deepcopy(ROW)
    row['duration'] = '03:00'
    result, _ = search(row=row)
    assert result.items[0]['arrival_at'] is None


@pytest.mark.asyncio
async def test_arrival_tool_schema_and_shared_count():
    from agents.contracts import RunState
    from travel_data.tools import ToolExecutor
    from evals.simulated_travel import SimulatedTrainProvider
    executor = ToolExecutor({'train_search': SimulatedTrainProvider(duration_minutes=600, departure_hours=[20])})
    assert 'train_search_by_arrival' in {s['function']['name'] for s in executor.schemas()}
    run = RunState('arrive')
    result = await executor.execute('train_search_by_arrival', {'origin': '重庆', 'destination': '上海',
        'arrival_date': '2026-10-18'}, run, call_id='a1')
    assert result['items'][0]['arrival_at'].startswith('2026-10-18')
    assert run.external_request_count == 2
