from agents.lazy_agent_registry import LazyAgentRegistry


def test_new_and_existing_dispatch_names_are_preserved():
    registry = LazyAgentRegistry(None, {})
    expected = {'train_search', 'hotel_search', 'travel_guide', 'rag_knowledge', 'memory_query', 'preference', 'information_query', 'itinerary_planning', 'event_collection'}
    assert expected <= set(registry.keys())
    assert all(name in registry for name in expected)
    for name in ('train_search', 'hotel_search', 'travel_guide'):
        assert registry[name] is registry[name]


def test_provider_injection_also_works_for_skill_directory_name():
    provider = object()
    registry = LazyAgentRegistry(None, {}, providers={'train_search': provider})
    assert registry['train-search'].provider is provider
