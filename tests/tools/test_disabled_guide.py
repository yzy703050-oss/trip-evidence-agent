from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedHotelProvider
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.tools.registry import build_registry


@pytest.mark.parametrize("tool_name", ["query", "write_trip"])
def test_disabled_guide_rejected_by_public_schema_and_runtime_contract(tool_name):
    tool = build_registry({})[tool_name]
    item = {"domain": "guide", "parameters": {"destination": "上海"}}
    args = (
        {"request_kind": "lookup", "query_plan": [item]}
        if tool_name == "query"
        else {
            "request_kind": "planning",
            "tripplan": {
                "operation": "propose", "default_round_trip": False,
                "tasks": [{"origin": "重庆", "destination": "上海", "items": [item]}],
            },
        }
    )
    assert not Draft202012Validator(tool.schema["function"]["parameters"]).is_valid(args)
    with pytest.raises(ValueError):
        tool.validate(args)


@pytest.mark.parametrize(
    "domain,parameters",
    [
        ("train", {"origin": "重庆", "destination": "上海", "departure_date": "2026-10-21"}),
        ("hotel", {"city": "上海"}),
        ("weather", {"city": "上海"}),
        ("web", {"query": "上海地铁服务公告"}),
    ],
)
def test_other_query_domains_keep_native_schema_and_validation(domain, parameters):
    tool = build_registry({})["query"]
    args = {"request_kind": "lookup", "query_plan": [{"domain": domain, "parameters": parameters}]}
    assert Draft202012Validator(tool.schema["function"]["parameters"]).is_valid(args)
    assert tool.validate(args)["query_plan"][0]["domain"] == domain


@pytest.mark.asyncio
async def test_disabled_guide_rejects_batch_before_queries_or_trip_writes(tmp_path):
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": hotel})
    calls = [
        {
            "id": "trip", "name": "write_trip",
            "arguments": {
                "request_kind": "planning",
                "tripplan": {
                    "operation": "propose", "default_round_trip": False,
                    "tasks": [{
                        "origin": "重庆", "destination": "上海", "items": [{"domain": "train"}],
                    }],
                },
            },
        },
        {
            "id": "queries", "name": "query",
            "arguments": {
                "request_kind": "planning",
                "query_plan": [
                    {"domain": "hotel", "parameters": {"city": "上海"}},
                    {"domain": "guide", "parameters": {"destination": "上海"}},
                ],
            },
        },
    ]
    before = deepcopy(calls)
    result = await execute_batch(
        calls, registry=runtime.registry,
        turn={"turn_id": "disabled-guide", "session_id": "s"},
        budget=TurnBudget(runtime.settings), sessions=runtime.sessions,
    )
    assert all(row["status"] == "rejected" for row in result)
    assert hotel.calls == []
    assert not runtime.trip_service.store.list_all()
    assert calls == before
