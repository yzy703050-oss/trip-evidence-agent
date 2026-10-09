import pytest
from agents.orchestration_agent import normalize_schedule


def test_dependencies_and_deduplication():
    rows = normalize_schedule([
        {'agent_name': 'information_query', 'requested_domains': ['train']},
        {'agent_name': 'information_query', 'requested_domains': ['hotel']},
        {'agent_name': 'preference'}, {'agent_name': 'memory_query'}])
    info = next(r for r in rows if r['agent_name'] == 'information_query')
    assert len(rows) == 3
    assert info['requested_domains'] == ['train', 'hotel']
    assert set(info['depends_on']) == {'preference', 'memory_query'}
    assert info['priority'] == 2


@pytest.mark.parametrize('name', ['train_search', 'hotel_search', 'travel_guide', 'event_collection', 'itinerary_planning'])
def test_retired_nodes_are_rejected(name):
    with pytest.raises(ValueError): normalize_schedule([{'agent_name': name}])


def test_same_phase_explicit_dependency_is_preserved():
    rows = normalize_schedule([{'agent_name': 'rag_knowledge', 'depends_on': ['memory_query']}, {'agent_name': 'memory_query'}])
    assert rows[0]['depends_on'] == ['memory_query']
