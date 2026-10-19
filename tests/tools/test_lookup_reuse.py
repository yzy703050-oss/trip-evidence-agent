from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedHotelProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [[7, 8, 9, 10, 11, 12, 14, 16], [7, 8]])
async def test_later_ranking_reprojects_cached_evidence_without_soft_filter_or_fabrication(
    tmp_path, hours
):
    from evals.simulated_travel import SimulatedHighSpeedProvider
    from trip_assistant.runtime.limits import TurnBudget

    provider = SimulatedHighSpeedProvider(duration_minutes=420, departure_hours=hours)
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    turn = {"budget": TurnBudget(runtime.settings)}
    item = {
        "domain": "train",
        "parameters": {
            "origin": "上海", "destination": "重庆", "departure_date": "2026-10-22",
        },
        "ranking": [{"field": "departure_at", "direction": "asc"}],
    }
    first = await runtime.queries.fetch(item, turn)
    soft = await runtime.queries.fetch(
        {**item, "constraints": {"departure_time_after": "12:00"},
         "condition_strengths": {"departure_time_after": "soft"}}, turn,
    )
    later = await runtime.queries.fetch(
        {**item, "ranking": [{"field": "departure_at", "direction": "desc"}]}, turn,
    )
    assert len(provider.calls) == 1
    assert first["query_id"] == soft["query_id"] == later["query_id"]
    assert first["result_revision"] == later["result_revision"] == 1
    assert first["items"] == soft["items"]
    assert first["items"][0]["departure_at"][11:16] == "07:00"
    assert later["items"][0]["departure_at"][11:16] == (
        "16:00" if len(hours) == 8 else "08:00"
    )
    assert later["candidate_total"] == len(hours)
    assert len(later["items"]) == min(5, len(hours))


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("separate_turn", [False, True])
async def test_overlapping_refresh_shares_one_raw_attempt_and_later_refresh_still_runs(
    tmp_path, failed, separate_turn
):
    import asyncio

    from trip_assistant.runtime.limits import TurnBudget

    class GatedHotel(SimulatedHotelProvider):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.count = 0

        async def search(self, query):
            self.count += 1
            self.entered.set()
            await self.release.wait()
            if failed:
                raise ValueError("fixture failure")
            return await super().search(query)

    provider = GatedHotel()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    turn = {"turn_id": "t", "budget": TurnBudget(runtime.settings)}
    item = {"domain": "hotel", "parameters": {"city": "上海"}, "refresh": True}
    ascending = {**item, "ranking": [{"field": "hotel_name", "direction": "asc"}]}
    descending = {**item, "ranking": [{"field": "hotel_name", "direction": "desc"}]}
    first = asyncio.create_task(runtime.queries.fetch(ascending, turn))
    await provider.entered.wait()
    other = {"turn_id": "other", "budget": TurnBudget(runtime.settings)} if separate_turn else turn
    second = asyncio.create_task(runtime.queries.fetch(descending, other))
    await asyncio.sleep(0)
    provider.release.set()
    a, b = await asyncio.gather(first, second)
    assert provider.count == 1
    if failed:
        assert a["status"] == b["status"] == "error"
    else:
        assert a["result_revision"] == b["result_revision"] == 1
        assert a["items"][0]["id"] != b["items"][0]["id"]
    await runtime.queries.fetch(item, turn)
    assert provider.count == 2


@pytest.mark.asyncio
async def test_successful_refresh_clears_previous_turn_failure(tmp_path):
    from trip_assistant.runtime.limits import TurnBudget

    class RecoveringHotel(SimulatedHotelProvider):
        failed = True

        async def search(self, query):
            if self.failed:
                raise ValueError("fixture failure")
            return await super().search(query)

    provider = RecoveringHotel()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    turn = {"budget": TurnBudget(runtime.settings)}
    item = {"domain": "hotel", "parameters": {"city": "上海"}}
    assert (await runtime.queries.fetch(item, turn))["status"] == "error"
    provider.failed = False
    assert (await runtime.queries.fetch({**item, "refresh": True}, turn))["status"] == "ok"
    assert (await runtime.queries.fetch(item, turn))["status"] == "ok"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_place_identity_ignores_stay_but_retains_brand_and_refresh(tmp_path):
    provider = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    item = {"domain": "hotel", "parameters": {"city": "上海", "keywords": "如家"}}
    turn = {
        "turn_id": "t",
        "budget": __import__("trip_assistant.runtime.limits", fromlist=["TurnBudget"]).TurnBudget(
            runtime.settings
        ),
    }
    first = await runtime.queries.fetch(item, turn)
    dated = deepcopy(item)
    dated["parameters"].update(check_in="2026-10-17", check_out="2026-10-19", guests=1)
    second = await runtime.queries.fetch(dated, turn)
    assert len(provider.calls) == 1
    assert second["query_id"] == first["query_id"] and second["cache_hit"]
    assert (
        runtime.queries.expand_queries({"query_plan": [item, dated]}, turn)[0]
        == (runtime.queries.expand_queries({"query_plan": [item, dated]}, turn)[1])
    )
    changed = deepcopy(dated)
    changed["parameters"]["keywords"] = "全季"
    await runtime.queries.fetch(changed, turn)
    assert len(provider.calls) == 2
    dated["refresh"] = True
    fresh = await runtime.queries.fetch(dated, turn)
    assert len(provider.calls) == 3 and fresh["result_revision"] == 2


@pytest.mark.asyncio
async def test_failed_same_turn_request_not_retried_by_hidden_planning(tmp_path):
    from trip_assistant.providers.contracts import AgentDataResult
    from trip_assistant.runtime.limits import TurnBudget

    class Failed:
        hotel_places = True
        calls = 0

        async def search(self, query):
            self.calls += 1
            return AgentDataResult("unavailable", query.to_dict(), [], [], None, None, "无资料")

    provider = Failed()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    item = {"domain": "hotel", "parameters": {"city": "上海"}}
    turn = {"turn_id": "t", "budget": TurnBudget(runtime.settings)}
    await runtime.queries.fetch(item, turn)
    again = await runtime.queries.fetch(item, turn)
    assert provider.calls == 1 and again["status"] == "unavailable"
    await runtime.queries.fetch({**item, "refresh": True}, turn)
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_expired_and_other_user_evidence_not_reused(tmp_path):
    from trip_assistant.runtime.limits import TurnBudget

    provider = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    turn = {"budget": TurnBudget(runtime.settings)}
    item = {"domain": "hotel", "parameters": {"city": "上海"}}
    await runtime.queries.fetch(item, turn)
    runtime.queries.now = lambda: (datetime.now(UTC) + timedelta(days=2)).isoformat()
    await runtime.queries.fetch(item, {"budget": TurnBudget(runtime.settings)})
    assert len(provider.calls) == 2
    other = NativeRuntimeFixture(tmp_path / "other", {"hotel": provider})
    await other.queries.fetch(item, {"budget": TurnBudget(runtime.settings)})
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_reranking_uses_complete_cache_instead_of_previous_top_five(tmp_path):
    from dataclasses import replace

    from trip_assistant.runtime.limits import TurnBudget

    class ManyPlaces(SimulatedHotelProvider):
        async def search(self, query):
            result = await super().search(query)
            items = [
                {**deepcopy(result.items[0]), "id": f"place_{i}", "hotel_name": f"如家{i}"}
                for i in range(10)
            ]
            return replace(result, items=items)

    provider = ManyPlaces()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": provider})
    turn = {"budget": TurnBudget(runtime.settings)}
    item = {
        "domain": "hotel",
        "parameters": {"city": "上海", "keywords": "如家"},
        "ranking": [{"field": "hotel_name", "direction": "asc"}],
    }
    first = await runtime.queries.fetch(item, turn)
    second = await runtime.queries.fetch(
        {**item, "ranking": [{"field": "hotel_name", "direction": "desc"}]}, turn
    )
    assert [i["id"] for i in first["items"]] == [f"place_{i}" for i in range(5)]
    assert [i["id"] for i in second["items"]] == [f"place_{i}" for i in range(9, 4, -1)]
    assert len(provider.calls) == 1
    assert len(runtime.evidence.get(first["query_id"])["items"]) == 10
