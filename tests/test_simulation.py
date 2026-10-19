"""Offline regression scenarios grouped by responsibility."""

import importlib
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from trip_assistant.providers.contracts import TrainQuery
from trip_assistant.providers.hotel_places import HotelPlaceQuery

# api_shaped_simulation


NOW = datetime(2026, 10, 13, 9, tzinfo=timezone(timedelta(hours=8)))


def providers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("API-shaped simulation must not use network")

    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    assert importlib.util.find_spec("evals.api_shaped_simulation") is not None, (
        "raw API simulation is missing"
    )
    return importlib.import_module("evals.api_shaped_simulation")


@pytest.mark.asyncio
async def test_raw_train_response_parses_multiple_seats_and_inventory(monkeypatch):
    api = providers(monkeypatch)
    provider = api.ApiShapedTrainProvider(now_fn=lambda: NOW)
    result = await provider.search(TrainQuery("重庆", "上海", date(2026, 10, 15)))
    assert result.status == "partial"  # Deliberately invalid row is removed.
    raw = provider.fixtures[0]["response"]
    assert raw["error_code"] == 0 and len(raw["result"]) >= 4
    assert {s["seat_name"] for s in raw["result"][0]["prices"]} >= {"二等座", "一等座"}
    available = next(i for i in result.items if i["availability"] == "available")
    assert available["price_cny"] == "584.50"
    assert available["origin_station"] == "重庆西"
    assert available["destination_station"] == "上海虹桥"
    assert available["arrival_at"] == "2026-10-15T18:50:00+08:00"
    assert available["time_evidence"]["arrival"] == "provider_segment_duration"
    sold = next(i for i in result.items if i["availability"] == "sold_out")
    assert sold["remaining"] == 0
    assert any(i["availability"] == "unknown" for i in result.items)
    assert all(i["source"]["provider"] == "simulation:juhe-api" for i in result.items)
    exported = json.dumps(provider.fixtures, ensure_ascii=False)
    assert "key" not in provider.fixtures[0]["request"]["data"]
    assert "offline-fixture-key" not in exported
    assert provider.fixtures[0]["parsed"] == result.to_dict()


@pytest.mark.asyncio
async def test_train_fixture_overnight_and_return_route_use_actual_parser(monkeypatch):
    api = providers(monkeypatch)
    provider = api.ApiShapedTrainProvider(now_fn=lambda: NOW)
    result = await provider.search(TrainQuery("上海", "重庆", date(2026, 10, 17)))
    overnight = next(i for i in result.items if i["departure_time"] == "19:00")
    assert overnight["arrival_at"] == "2026-10-18T07:30:00+08:00"
    assert overnight["origin_station"] == "上海虹桥"
    assert overnight["destination_station"] == "重庆西"


@pytest.mark.asyncio
async def test_raw_amap_response_cleans_duplicates_and_never_invents_quotes(monkeypatch):
    api = providers(monkeypatch)
    provider = api.ApiShapedHotelProvider()
    query = HotelPlaceQuery("上海", "全季", date(2026, 10, 15), date(2026, 10, 17), 1)
    result = await provider.search(query)
    raw = provider.fixtures[0]["response"]
    assert raw["status"] == "1" and raw["infocode"] == "10000"
    assert raw["pois"][0]["business"]["cost"] == "350"
    assert result.status == "partial" and len(result.items) == 3
    assert len({i["id"] for i in result.items}) == 3
    assert all(i["kind"] == "hotel_place" for i in result.items)
    assert all(i["stay_total_cny"] is None and i["availability"] == "unknown" for i in result.items)
    assert all(i["source"]["provider"] == "simulation:amap-api" for i in result.items)
    assert result.items[0]["rating"] == "4.5" and result.items[0]["phone"]
    assert provider.fixtures[0]["parsed"] == result.to_dict()
    assert "key" not in provider.fixtures[0]["request"]["params"]


# simulated_travel


@pytest.mark.asyncio
async def test_simulated_trains_are_valid_candidates_with_multiple_options():
    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.providers.validation import guard_domain_result

    provider = SimulatedTrainProvider()
    result = await provider.search(TrainQuery("重庆", "上海", date(2026, 10, 17)))
    checked = guard_domain_result("train", result.to_dict())
    assert checked["status"] == "ok"
    assert len(checked["items"]) >= 3
    assert all(
        i["source"]["provider"] == "simulation:train" and i["arrival_at"] for i in checked["items"]
    )
    assert "模拟" in result.message


@pytest.mark.asyncio
async def test_simulated_hotels_remain_places_without_prices_or_inventory():
    from evals.simulated_travel import SimulatedHotelProvider
    from trip_assistant.providers.validation import guard_domain_result

    result = await SimulatedHotelProvider().search(HotelPlaceQuery("上海"))
    checked = guard_domain_result("hotel", result.to_dict())
    assert checked["status"] == "ok"
    assert len(checked["items"]) >= 3
    assert all(
        i["stay_total_cny"] is None and i["availability"] == "unknown" for i in checked["items"]
    )
    assert result.source.provider == "simulation:hotel"
