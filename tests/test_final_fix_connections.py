import asyncio
import pytest
from agents.contracts import RunState
from agents.orchestration_agent import normalize_schedule
from travel_data.tools import ToolExecutor
from travel_data.contracts import AgentDataResult


class Provider:
    def __init__(self, status='ok', fail=False): self.queries=[]; self.status=status; self.fail=fail
    async def search(self, query):
        self.queries.append(query)
        if self.fail: raise TimeoutError('secret')
        return AgentDataResult(self.status, query.to_dict(), [], [], None, None, None)


@pytest.mark.parametrize('priority', [float('inf'), -float('inf'), float('nan'), True])
def test_invalid_schedule_priority_is_rejected(priority):
    with pytest.raises(ValueError): normalize_schedule([{'agent_name': 'information_query', 'priority': priority}])


@pytest.mark.asyncio
async def test_separate_passenger_guest_queries():
    train, hotel = Provider(), Provider()
    executor = ToolExecutor({'train_search': train, 'hotel_search': hotel})
    run = RunState('a')
    await executor.execute('train_search', {'origin': 'Shanghai', 'destination': 'Beijing', 'departure_date': '2026-10-20', 'passengers': 2}, run, call_id='1')
    await executor.execute('hotel_search', {'city': 'Beijing', 'check_in': '2026-10-20', 'check_out': '2026-10-22', 'guests': 1}, run, call_id='2')
    assert train.queries[0].passengers == 2 and hotel.queries[0].guests == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['error', 'partial', 'needs_input', 'unavailable'])
async def test_business_status_keeps_successful_sibling(status):
    executor = ToolExecutor({'train_search': Provider(status, status == 'error'), 'travel_guide': Provider()})
    run = RunState('a')
    await asyncio.gather(executor.execute('train_search', {'origin': 'Shanghai', 'destination': 'Beijing', 'departure_date': '2026-10-20'}, run, call_id='1'), executor.execute('travel_guide', {'destination': 'Beijing'}, run, call_id='2'))
    assert run.domain_results['train']['status'] == status
    assert run.domain_results['guide']['status'] == 'ok'
