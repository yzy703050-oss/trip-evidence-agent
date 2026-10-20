import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.limits import TurnBudget


@pytest.mark.parametrize("domain", ["train", "hotel"])
@pytest.mark.parametrize("failed", [False, True])
async def test_fetch_prepares_once_without_changing_cache_or_failure_behavior(
    tmp_path, monkeypatch, domain, failed
):
    provider = SimulatedTrainProvider() if domain == "train" else SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {domain: provider})
    parameters = (
        {"origin": "重庆", "destination": "上海", "departure_date": "2026-10-23"}
        if domain == "train" else {"city": "上海"}
    )
    item = {"domain": domain, "parameters": parameters}
    original = runtime.queries.prepare
    preparations = []

    def prepare(value):
        preparations.append(value)
        return original(value)

    if failed:
        async def unavailable(query):
            raise ValueError("fixture unavailable")
        monkeypatch.setattr(provider, "search", unavailable)
    monkeypatch.setattr(runtime.queries, "prepare", prepare)
    turn = {"budget": TurnBudget(runtime.settings)}
    first = await runtime.queries.fetch(item, turn)
    assert len(preparations) == 1
    preparations.clear()
    second = await runtime.queries.fetch(item, turn)
    assert len(preparations) == 1
    assert first["status"] == second["status"] == ("error" if failed else "ok")
    assert first["query"] == second["query"]
    if not failed:
        assert len(provider.calls) == 1
        assert first["query_id"] == second["query_id"]
        assert second["cache_hit"]
