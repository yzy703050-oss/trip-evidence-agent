"""Main agent itinerary ability; no independent model or agent lifecycle."""
from travel_data.plan_guard import guard_itinerary


def build_itinerary_context(context: dict) -> dict:
    return {key: context.get(key) for key in ('original_query', 'rewritten_query',
            'effective_preferences', 'travel_conditions', 'domain_results', 'results')}


def guard_final_itinerary(plan: dict, domain_results: dict) -> dict:
    mapping = {'train': 'train_search', 'hotel': 'hotel_search', 'guide': 'travel_guide'}
    rows = [{'agent_name': mapping[domain], 'result': {'data': value}}
            for domain, value in domain_results.items() if domain in mapping]
    return guard_itinerary(plan, rows)
