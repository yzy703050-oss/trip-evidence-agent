import json
from copy import deepcopy
from datetime import date

import pytest


@pytest.mark.asyncio
async def test_arrival_search_finds_previous_day_departure_and_preserves_evidence():
    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.providers.arrival_search import search_by_arrival

    provider = SimulatedTrainProvider(duration_minutes=600, departure_hours=[20])
    result = await search_by_arrival(
        provider,
        {"origin": "重庆", "destination": "上海", "arrival_date": "2026-10-18", "passengers": 2},
        request_budget=3,
    )
    assert result.status == "ok"
    assert provider.calls == [date(2026, 10, 18), date(2026, 10, 17)]
    assert result.items[0]["departure_at"].startswith("2026-10-17T20:00")
    assert result.items[0]["arrival_at"].startswith("2026-10-18T06:00")
    assert result.items[0]["source"]["provider"].startswith("simulation:")


@pytest.mark.asyncio
async def test_arrival_deadline_filters_same_day_late_arrivals():
    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.providers.arrival_search import search_by_arrival

    provider = SimulatedTrainProvider(duration_minutes=120, departure_hours=[8, 14])
    result = await search_by_arrival(
        provider,
        {
            "origin": "北京",
            "destination": "上海",
            "arrival_date": "2026-10-18",
            "arrival_before": "2026-10-18T12:00:00+08:00",
        },
        request_budget=3,
    )
    assert result.items and all(i["arrival_time"] == "10:00" for i in result.items)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_arrival_search_obeys_external_request_budget():
    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.providers.arrival_search import search_by_arrival

    provider = SimulatedTrainProvider(duration_minutes=1800, departure_hours=[20])
    result = await search_by_arrival(
        provider,
        {"origin": "重庆", "destination": "上海", "arrival_date": "2026-10-18"},
        request_budget=1,
    )
    assert len(provider.calls) == 1
    assert result.status == "partial"
    assert result.items == []


@pytest.mark.asyncio
async def test_unavailable_provider_is_not_repeated_for_other_dates():
    from trip_assistant.providers.arrival_search import search_by_arrival
    from trip_assistant.providers.contracts import AgentDataResult

    class Provider:
        calls = 0

        async def search(self, query):
            self.calls += 1
            return AgentDataResult("unavailable", query.to_dict(), [], [], None, None, "配额耗尽")

    provider = Provider()
    result = await search_by_arrival(
        provider,
        {"origin": "重庆", "destination": "上海", "arrival_date": "2026-10-18"},
        request_budget=3,
    )
    assert provider.calls == 1
    assert result.status == "unavailable"
    assert result.items == []


def test_duration_adapter_calculates_cross_year_and_more_than_24_hours():
    from support.juhe_train_provider import ROW, search

    row = deepcopy(ROW)
    row.update(departure_time="23:00", arrival_time="01:30", duration="26:30")
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 12, 31, 9, tzinfo=timezone(timedelta(hours=8)))
    result, _ = search(row=row, day=now.date(), now_fn=lambda: now)
    assert result.items[0]["arrival_at"] == "2027-01-02T01:30:00+08:00"
    assert result.items[0]["time_evidence"]["arrival"] == "provider_segment_duration"


def test_inconsistent_duration_does_not_create_false_arrival_date():
    from support.juhe_train_provider import ROW, search

    row = deepcopy(ROW)
    row["duration"] = "03:00"
    result, _ = search(row=row)
    assert result.items[0]["arrival_at"] is None


@pytest.mark.asyncio
async def test_arrival_tool_schema_and_shared_count(tmp_path):
    from types import SimpleNamespace

    from support.native import NativeRuntimeFixture

    from evals.simulated_travel import SimulatedTrainProvider

    provider = SimulatedTrainProvider(duration_minutes=600, departure_hours=[20])
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    parameters = {
        "origin": "重庆",
        "destination": "上海",
        "arrival_date": "2026-10-18",
        "passengers": 1,
    }
    args = {"request_kind": "lookup", "query_plan": [{"domain": "train", "parameters": parameters}]}
    validated = runtime.registry["query"].validate(args)
    assert len(runtime.queries.expand_queries(validated, {})) == 3
    runtime.model.script = [
        SimpleNamespace(
            content=[{"type": "tool_use", "id": "arrive", "name": "query", "input": args}]
        ),
        "已取得到达日期匹配的候选。",
    ]
    result = await runtime.run("查10月18日抵达上海的车次")
    assert result["tool_results"][0]["data"][0]["items"][0]["arrival_at"].startswith("2026-10-18")
    assert len(provider.calls) == 2 and len(runtime.model.messages) == 2
    assert "secret-token" not in json.dumps(result)
