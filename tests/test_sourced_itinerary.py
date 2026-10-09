import json
from travel_data.plan_guard import guard_itinerary


def test_guard_removes_model_facts_and_unknown_ids():
    plan = {"itinerary": {"title": "299元车票", "notes": ["今日开放"], "estimated_budget": "299元", "daily_plans": [{"day": 1, "activities": [{"time": "09:00", "location": "故宫", "description": "今日开放299元", "notes": "已预约"}]}]}, "selected_train_id": "fake"}
    guarded = guard_itinerary(plan, [])
    assert "299" not in json.dumps(guarded, ensure_ascii=False)
    assert "今日开放" not in json.dumps(guarded, ensure_ascii=False)
    assert guarded["selected_train_id"] is None
    assert guarded["budget"]["complete"] is False
    assert guarded["itinerary"]["daily_plans"][0]["activities"] == []


def test_guard_uses_real_offer_and_confirmed_passengers():
    source = {"provider": "authorized", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com"}
    offer = {"id": "t1", "train_number": "G1", "origin_station": "上海", "destination_station": "北京", "departure_time": "09:00", "arrival_time": "14:00", "seat_class": "二等座", "price_cny": "123.45", "availability": "available", "remaining": None, "source": source, "url": None}
    results = [{"agent_name": "train_search", "result": {"data": {"status": "ok", "query": {"passengers": 2}, "items": [offer]}}}, {"agent_name": "hotel_search", "result": {"data": {"status": "unavailable", "items": []}}}]
    guarded = guard_itinerary({"selected_train_id": "t1", "passengers": 9}, results)
    assert guarded["selected_train"]["price_cny"] == "123.45"
    assert guarded["budget"]["known_subtotal_cny"] == "246.90"
    assert "hotel" in guarded["budget"]["missing_categories"]


def test_guide_facts_and_hotel_are_rebuilt_with_item_sources():
    source = {"provider": "official", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com/official"}
    hotel = {"id": "h1", "hotel_name": "全季", "room_type": "标准房", "check_in": "2026-10-08", "check_out": "2026-10-10", "guests": 2, "stay_total_cny": "500.00", "fees_included": True, "availability": "available", "cancellation": "不可取消", "source": source, "url": None}
    facts = [{"kind": "opening", "content": "09:00-17:00", "verification": "verified", "source": source}, {"kind": "booking", "content": "待核实", "verification": "needs_check", "source": None}]
    rows = [{"agent_name": "hotel_search", "result": {"data": {"status": "ok", "items": [hotel]}}}, {"agent_name": "travel_guide", "result": {"data": {"status": "partial", "items": facts}}}]
    plan = guard_itinerary({"selected_hotel_id": "h1", "weather": "晴", "booking": "无需预约"}, rows)
    assert plan["selected_hotel"] == hotel
    assert plan["guide_facts"] == facts
    assert plan["budget"]["known_subtotal_cny"] == "500.00"
    assert "weather" not in plan and "booking" not in plan


def test_invalid_source_and_malformed_model_fields_are_discarded():
    rows = [{"agent_name": "train_search", "result": {"data": {"status": "ok", "items": [{"id": "bad", "price_cny": "299", "source": {"provider": "bad", "fetched_at": "2026-10-08T10:00:00"}}]}}}]
    plan = guard_itinerary({"selected_train_id": [], "itinerary": {"daily_plans": None}}, rows)
    assert plan["selected_train"] is None
    assert plan["itinerary"]["daily_plans"] == []


import pytest

@pytest.mark.asyncio
async def test_main_applies_guard_to_actual_model_response():
    from types import SimpleNamespace
    from agents.main_agent import MainAgent
    from agents.execution_harness import ExecutionHarness
    from agents.contracts import RunState
    async def model(messages):
        return SimpleNamespace(text=json.dumps({'action': 'itinerary', 'itinerary': {'notes': ['299'], 'daily_plans': [{'city': 'forged 299'}]}}))
    main = MainAgent(model)
    harness = ExecutionHarness(main, {})
    result = await harness._finalize({'response_mode': 'itinerary'}, RunState('t1'), None)
    assert '299' not in json.dumps(result)
    assert result['itinerary']['budget']['complete'] is False


@pytest.mark.parametrize("field", ["city", "location"])
def test_guard_rejects_factual_prose_in_suggestion_fields(field):
    injected = "故宫门票两百块，现有库存充足"
    day = {"day": 1, "city": injected if field == "city" else "北京", "activities": [{"location": injected if field == "location" else "故宫"}]}
    plan = guard_itinerary({"itinerary": {"daily_plans": [day]}}, [])
    assert injected not in json.dumps(plan, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['timeout', 'parse', 'exception'])
async def test_main_failure_preserves_safe_error(failure):
    from types import SimpleNamespace
    from agents.main_agent import MainAgent
    from agents.execution_harness import ExecutionHarness
    from agents.contracts import RunState
    async def model(messages):
        if failure == 'parse': return SimpleNamespace(text='unparsable arbitrary prose')
        raise TimeoutError('secret arbitrary error prose')
    result = await ExecutionHarness(MainAgent(model), {})._finalize({'response_mode': 'itinerary'}, RunState('t1'), None)
    assert result['status'] == 'error'
    assert 'arbitrary' not in json.dumps(result)
    assert 'itinerary' not in result


def test_places_only_use_provider_references_or_fixed_generic_suggestions():
    source = {"provider": "maps", "fetched_at": "2026-10-08T10:00:00+08:00", "url": "https://example.com"}
    rows = [{"agent_name": "travel_guide", "result": {"data": {"status": "ok", "query": {"destination": "北京"}, "items": [{"kind": "place", "content": "故宫", "verification": "verified", "source": source}]}}}]
    plan = guard_itinerary({"itinerary": {"daily_plans": [{"city_ref": "guide_destination", "city": "恶意散文", "activities": [{"location_ref": "guide:0", "location": "恶意散文"}, {"location_ref": "suggestion:city_walk"}]}]}}, rows)
    day = plan["itinerary"]["daily_plans"][0]
    assert day["city"] == "北京"
    assert day["activities"][0]["location"] == "故宫"
    assert day["activities"][1]["location"] == "市内漫步（地点待核实）"
    assert guard_itinerary(plan, rows) == plan
