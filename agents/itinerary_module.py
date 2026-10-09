"""Main agent itinerary ability; no independent model or agent lifecycle."""
from travel_data.plan_guard import guard_itinerary


def build_itinerary_context(context: dict) -> dict:
    return {key: context.get(key) for key in ('original_query', 'rewritten_query',
            'effective_preferences', 'travel_conditions', 'domain_results', 'results')}


def guard_final_itinerary(plan: dict, domain_results: dict, conditions: dict | None = None) -> dict:
    mapping = {'train': 'train_search', 'hotel': 'hotel_search', 'guide': 'travel_guide'}
    rows = [{'agent_name': mapping[domain], 'result': {'data': value}}
            for domain, value in domain_results.items() if domain in mapping]
    guarded = guard_itinerary(plan, rows)
    conditions = conditions or {}
    days = guarded['itinerary']['daily_plans']
    missing = list(conditions.get('missing_fields', []))
    if not days or any(not day.get('date') for day in days):
        missing.append('dates')
    if not (conditions.get('destination') or conditions.get('city') or any(day.get('city') for day in days)):
        missing.append('destination')
    guarded['missing_fields'] = sorted(set(missing))
    if missing:
        guarded['planning_complete'] = False
    return guarded
