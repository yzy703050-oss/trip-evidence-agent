from collections import Counter

import pytest
from support.native import NativeRuntimeFixture

from trip_assistant.memory.planning import review_payload
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.storage.trips import TripStore


@pytest.mark.parametrize("targets", [0, 1, 2])
async def test_review_reuses_one_event_snapshot_and_loads_each_target_once(
    tmp_path, monkeypatch, targets
):
    runtime = NativeRuntimeFixture(tmp_path)
    turn = {"turn_id": "review", "original_query": "分别查看两个行程及酒店查询失败原因"}
    identities = []
    for index in range(targets):
        result = await runtime.trip_service.apply(
            {
                "operation": "propose",
                "default_round_trip": False,
                "tasks": [{"origin": "重庆", "destination": "上海", "items": []}],
            },
            {**turn, "operation_id": f"seed-{index}", "budget": TurnBudget(runtime.settings)},
        )
        identities.append(result["trip_plan_id"])
    runtime.sessions.record_calls(
        [{"id": "lookup", "name": "query", "arguments": {
            "request_kind": "lookup", "query_plan": [
                {"domain": "hotel", "parameters": {"city": "上海"}},
            ],
        }}],
        turn["turn_id"],
    )
    runtime.sessions.record_result("lookup", {
        "call_id": "lookup", "name": "query", "status": "partial", "data": [],
        "reason": "供应商暂不可用",
    }, turn["turn_id"])
    target = identities if targets == 2 else identities[0] if targets else None
    expected = review_payload(runtime.sessions, turn, target)
    reads, loads = [], Counter()
    original_read = runtime.sessions.read_events
    original_load = TripStore.load

    def read_events(*args, **kwargs):
        reads.append(kwargs)
        return original_read(*args, **kwargs)

    def load(store, identity):
        loads[identity] += 1
        return original_load(store, identity)

    monkeypatch.setattr(runtime.sessions, "read_events", read_events)
    monkeypatch.setattr(TripStore, "load", load)
    actual = review_payload(runtime.sessions, turn, target)
    assert actual == expected
    assert actual["query_results"][0]["reason"] == "供应商暂不可用"
    assert actual["query_results"][0]["status"] == "partial"
    assert len(actual.get("tripplans", [])) == (2 if targets == 2 else 0)
    assert len(reads) == 1
    assert loads == Counter(identities)
