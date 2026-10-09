"""One-turn request deduplication and bounded candidate views."""
import asyncio
from copy import deepcopy
import json
from decimal import Decimal


class CandidateStore:
    def __init__(self):
        self.cache = {}
        self.locks = {}

    async def get_or_fetch(self, key, fetch):
        async with self.locks.setdefault(key, asyncio.Lock()):
            if key in self.cache:
                return self.cache[key], True
            result = await fetch()
            self.cache[key] = result
            return result, False


def query_cache_key(domain: str, query: dict) -> str:
    return domain + ':' + json.dumps(query, sort_keys=True, ensure_ascii=False)


def candidate_view(result, *, limit, offset, constraints, preferences):
    value = deepcopy(result.to_dict())
    kind = 'train' if 'departure_date' in value['query'] else 'hotel' if 'check_in' in value['query'] else None
    if not kind:
        return value
    items = []
    for item in value['items']:
        price_key = 'price_cny' if kind == 'train' else 'stay_total_cny'
        maximum = constraints.get(f'{kind}_max_total_cny')
        amount = Decimal(item[price_key]) if item.get(price_key) is not None else None
        if kind == 'train' and amount is not None:
            amount *= value['query'].get('passengers', 1)
        if maximum is not None and (amount is None or amount > Decimal(str(maximum))):
            continue
        nightly = constraints.get('hotel_max_nightly_cny') if kind == 'hotel' else None
        if nightly is not None:
            from datetime import date
            nights = (date.fromisoformat(item['check_out']) - date.fromisoformat(item['check_in'])).days
            if amount is None or amount / nights > Decimal(str(nightly)):
                continue
        if kind == 'train':
            if constraints.get('seat_class') and item.get('seat_class') != constraints['seat_class']:
                continue
            if constraints.get('departure_time_after') and item.get('departure_time', '') < constraints['departure_time_after']:
                continue
            if constraints.get('departure_time_before') and item.get('departure_time', '') > constraints['departure_time_before']:
                continue
        if constraints.get('available_only') and item.get('availability') != 'available':
            continue
        items.append(item)
    brands = preferences.get('hotel_brands') or []
    brands = [brands] if isinstance(brands, str) else brands if isinstance(brands, list) else []
    brands = [brand for brand in brands if isinstance(brand, str) and brand]
    def rank(item):
        preferred = (item.get('seat_class') == preferences.get('seat_preference')) if kind == 'train' else any(
            brand in item.get('hotel_name', '') for brand in brands)
        price = item.get('price_cny' if kind == 'train' else 'stay_total_cny')
        return (not preferred, price is None, Decimal(price) if price is not None else Decimal('0'))
    items.sort(key=rank)
    if kind == 'train':
        seen, unique, rest = set(), [], []
        for item in items:
            train = item.get('train_number')
            (rest if train in seen else unique).append(item)
            seen.add(train)
        items = unique + rest
    value['candidate_total'] = len(items)
    value['items'] = items[offset:offset+limit]
    return value
