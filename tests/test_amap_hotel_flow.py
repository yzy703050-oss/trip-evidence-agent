"""Exercise the existing tool, guard, budget and CLI with the real hotel provider."""

import io
from copy import deepcopy

import pytest
from rich.console import Console
from test_amap_hotel import Response, poi

from agents.contracts import RunState
from travel_data.amap_hotel import AmapHotelProvider
from travel_data.plan_guard import guard_itinerary
from travel_data.result_guard import guard_domain_result
from travel_data.tools import ToolExecutor


def executor():
    calls = []

    def get(*args, **kwargs):
        calls.append(kwargs["params"])
        return Response({"status": "1", "infocode": "10000", "pois": [poi(i) for i in range(7)]})

    return ToolExecutor({"hotel_search": AmapHotelProvider("test-secret", http_get=get)}), calls


@pytest.mark.asyncio
async def test_city_only_hotel_search_keeps_five_candidates_and_cached_next_window():
    tool, calls = executor()
    run = RunState("hotel")
    first = await tool.execute("hotel_search", {"city": "上海"}, run, call_id="first")
    assert first["status"] == "ok"
    assert first["missing_fields"] == []
    assert len(first["items"]) == 5 and first["candidate_total"] == 7
    second = await tool.execute(
        "hotel_search", {"city": "上海", "candidate_offset": 5}, run, call_id="second"
    )
    assert [item["id"] for item in second["items"]] == ["B5", "B6"]
    assert len(calls) == 1 and run.tool_requests[-1]["cache_hit"] is True
    assert "test-secret" not in str(run.tool_requests)


@pytest.mark.asyncio
async def test_place_schema_only_requires_city_and_exposes_keyword_search():
    tool, _ = executor()
    schema = next(
        item["function"] for item in tool.schemas() if item["function"]["name"] == "hotel_search"
    )
    assert schema["parameters"]["required"] == ["city"]
    assert "keywords" in schema["parameters"]["properties"]
    result = await tool.execute(
        "hotel_search", {"city": "上海", "keywords": "汉庭"}, RunState("keyword"), call_id="a"
    )
    assert result["status"] == "ok" and result["query"]["keywords"] == "汉庭"


@pytest.mark.asyncio
async def test_missing_city_does_not_request_amap():
    tool, calls = executor()
    result = await tool.execute("hotel_search", {}, RunState("missing"), call_id="a")
    assert result["status"] == "needs_input" and result["missing_fields"] == ["city"]
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"city": "上海", "keywords": " "},
        {"city": "上海", "guests": 0},
        {"city": "上海", "check_in": "bad"},
        {"city": "上海", "check_in": "2026-10-12", "check_out": "2026-10-10"},
    ],
)
async def test_invalid_provided_conditions_do_not_request_amap(arguments):
    tool, calls = executor()
    result = await tool.execute("hotel_search", arguments, RunState("invalid"), call_id="a")
    assert result["status"] == "needs_input" and calls == []


@pytest.mark.asyncio
async def test_budget_and_available_filters_keep_places_as_explicitly_unverified_references():
    tool, _ = executor()
    result = await tool.execute(
        "hotel_search",
        {"city": "上海", "constraints": {"hotel_max_nightly_cny": "500", "available_only": True}},
        RunState("budget"),
        call_id="a",
    )
    assert result["status"] == "partial" and len(result["items"]) == 5
    assert set(result["unverified_constraints"]) == {"hotel_max_nightly_cny", "available_only"}
    assert "未验证" in result["message"]
    assert all(
        item["stay_total_cny"] is None and item["availability"] == "unknown"
        for item in result["items"]
    )


@pytest.mark.asyncio
async def test_optional_stay_metadata_does_not_turn_a_place_into_a_quoted_room():
    tool, _ = executor()
    result = await tool.execute(
        "hotel_search",
        {"city": "上海", "check_in": "2026-10-10", "check_out": "2026-10-12", "guests": 2},
        RunState("stay"),
        call_id="a",
    )
    assert len(result["items"]) == 5
    assert result["query"]["guests"] == 2
    assert all(
        item["stay_total_cny"] is None and item["room_type"] is None for item in result["items"]
    )


@pytest.mark.asyncio
async def test_selected_hotel_place_survives_itinerary_guard_without_entering_known_budget():
    tool, _ = executor()
    result = await tool.execute("hotel_search", {"city": "上海"}, RunState("plan"), call_id="a")
    plan = guard_itinerary(
        {"selected_hotel_id": "B1"}, [{"agent_name": "hotel_search", "data": result}]
    )
    assert plan["selected_hotel"]["hotel_name"] == "汉庭酒店1"
    assert plan["selected_hotel"]["address"] == "示例路1号"
    assert plan["budget"]["known_subtotal_cny"] == "0"
    assert "hotel" in plan["budget"]["missing_categories"] and plan["budget"]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"availability": "available"},
        {"stay_total_cny": "350"},
        {"room_type": "大床房"},
    ],
)
async def test_place_cannot_acquire_price_or_inventory_claims_during_guarding(changed):
    tool, _ = executor()
    result = await tool.execute("hotel_search", {"city": "上海"}, RunState("forged"), call_id="a")
    assert result["items"], "real place results must exist before testing fabricated claims"
    altered = deepcopy(result)
    altered["items"] = [{**result["items"][0], **changed}]
    checked = guard_domain_result("hotel", altered)
    assert checked["status"] == "partial" and checked["items"] == []


@pytest.mark.asyncio
async def test_cli_displays_the_real_place_address_and_unknown_inventory():
    from cli import TripEvidenceCLI

    tool, _ = executor()
    result = await tool.execute("hotel_search", {"city": "上海"}, RunState("cli"), call_id="a")
    cli = TripEvidenceCLI()
    output = io.StringIO()
    cli.console = Console(file=output, width=160, color_system=None)
    cli._display_sourced_result("hotel_search", result)
    text = output.getvalue()
    assert "汉庭酒店1" in text and "示例路1号" in text and "amap" in text
    assert "房价、空房和入住规则尚未核实" in text and "test-secret" not in text


def test_cli_startup_registers_configured_hotel_provider_in_the_information_agent(monkeypatch):
    import asyncio

    from test_juhe_cli_wiring import initialize

    monkeypatch.setenv("AMAP_API_KEY", "test-secret")
    monkeypatch.setattr(
        "travel_data.amap_hotel.requests.get",
        lambda *a, **kw: Response({"status": "1", "infocode": "10000", "pois": [poi()]}),
    )
    app, output = initialize(monkeypatch, "")
    tool = app.harness.agent_registry["information_query"].tool_executor
    result = asyncio.run(
        tool.execute("hotel_search", {"city": "上海"}, RunState("startup"), call_id="a")
    )
    assert result["status"] == "ok" and result["items"][0]["hotel_name"] == "汉庭酒店1"
    from utils.response_renderer import finalize_business_result
    app._display_results(finalize_business_result({"domain_results": {"hotel": result}}))
    assert "汉庭酒店1" in output.getvalue() and "test-secret" not in output.getvalue()
