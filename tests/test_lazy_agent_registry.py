import pytest
from agents.lazy_agent_registry import LazyAgentRegistry


def test_registry_rejects_private_and_retired_nodes():
    registry = LazyAgentRegistry(None, {'train_search': object()})
    assert set(registry.keys()) == {'preference', 'memory_query', 'rag_knowledge', 'information_query'}
    for name in ('train_search', 'train-search', 'hotel_search', 'travel_guide', 'event_collection', 'plan-trip', 'query-info', 'ask-question'):
        assert name not in registry
        with pytest.raises(KeyError): registry[name]


def test_only_information_receives_tool_executor():
    provider = object()
    registry = LazyAgentRegistry(None, {}, providers={'train_search': provider})
    assert registry['information_query'].tool_executor.providers['train_search'] is provider
    assert not hasattr(registry['preference'], 'tool_executor')
    assert registry['information_query'] is registry['information_query']
