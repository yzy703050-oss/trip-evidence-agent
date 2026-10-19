"""Offline fixtures extracted from test_rail_preferences; no test-module imports."""

from copy import deepcopy

from trip_assistant.providers.candidates import candidate_view
from trip_assistant.providers.contracts import AgentDataResult


def offer(number, departure="10:00", arrival="16:00", price="100", **extra):
    return dict(
        id=number,
        train_number=number,
        departure_time=departure,
        arrival_time=arrival,
        price_cny=price,
        seat_class="二等座",
        **extra,
    )


def view(items, *, preferences=None, constraints=None, ranking=None, offset=0, limit=5):
    data = AgentDataResult(
        "ok", {"departure_date": "2026-10-20", "passengers": 1}, items, [], None, None, None
    )
    before = deepcopy(data.to_dict())
    result = candidate_view(
        data,
        limit=limit,
        offset=offset,
        constraints=constraints or {},
        preferences=preferences or {},
        ranking=ranking or [],
    )
    assert data.to_dict() == before
    return result
