"""Characterize strict first-proposal routing; prompt behavior is evaluated with a real model."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_consolidated_travel_run import BatchMain
from workflow_runtime import OfflineProvider

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from agents.main_agent import MainAgent
from context.memory_manager import MemoryManager
from context.telemetry import current_model_context


def proposal(case):
    route = [("重庆", "上海", "2026-10-14", 1)]
    if case == "multi":
        route += [("上海", "杭州", "2026-10-15", 3)]
    tasks = [
        dict(
            origin=a,
            destination=b,
            purpose="visit",
            requires_hotel=True,
            conditions={"departure_date": day, "nights": nights},
            field_sources={"departure_date": "user", "nights": "proposal"},
            condition_strengths={"departure_date": "hard", "nights": "soft"},
        )
        for a, b, day, nights in route
    ]
    if case == "dates":
        tasks[0]["conditions"] = {
            "departure_date": "2026-10-14",
            "check_in": "2026-10-14",
            "check_out": "2026-10-16",
        }
        tasks[0]["field_sources"] = {k: "user" for k in tasks[0]["conditions"]}
        tasks[0]["condition_strengths"] = {k: "hard" for k in tasks[0]["conditions"]}
    return_day = "2026-10-18" if case == "multi" else "2026-10-16"
    tasks.append(
        dict(
            origin=route[-1][1],
            destination="重庆",
            purpose="return",
            requires_hotel=False,
            conditions={"departure_date": return_day},
            field_sources={"departure_date": "user"},
        )
    )
    return dict(confirmed_conditions={}, tasks=tasks)


class Responses:
    def __init__(self, values):
        self.values = iter(values)
        self.stages = []

    async def __call__(self, messages, **kwargs):
        self.stages.append(current_model_context()["stage"])
        return SimpleNamespace(text=json.dumps(next(self.values), ensure_ascii=False))


class Main(MainAgent):
    def __init__(self, model):
        super().__init__(model)
        self.calls, self.views = 0, []

    async def select_batch(self, context):
        return await BatchMain.select_batch(self, context)


def setup_run(tmp_path, initial, repairs=()):
    decision = dict(
        intents=[{"type": "plan"}],
        response_mode="workflow",
        agent_schedule=[],
        workflow_proposal=initial,
    )
    responses = Responses([decision, *repairs])
    main = Main(responses)
    train, hotel = OfflineProvider("train"), OfflineProvider("hotel")

    class Registry(dict):
        providers = {"train_search": train, "hotel_search": hotel}

    harness = ExecutionHarness(main, Registry(), MemoryManager("test", "session", str(tmp_path)))
    context = {
        "original_query": "重庆出发去上海旅游，安排火车酒店",
        "current_time": "2026-10-13T10:00:00+08:00",
    }
    return harness, context, responses, train, hotel


@pytest.mark.parametrize("case", ["normal", "dates", "multi"])
async def test_valid_initial_proposal_queries_without_repair(tmp_path, case):
    harness, context, model, train, hotel = setup_run(tmp_path, proposal(case))
    result = await harness.run_turn(context, RunState("first-proposal"))
    assert result["status"] == "completed"
    assert model.stages == ["main:plan"]
    tasks = result["workflow"]["tasks"]
    assert len(train.calls) == len(tasks)
    assert len(hotel.calls) == len(tasks) - 1
    if case == "dates":
        assert tasks[0]["conditions"]["check_out"] == "2026-10-16"
        assert tasks[0]["field_sources"]["check_out"] == "user"
    else:
        assert [t["conditions"]["nights"] for t in tasks[:-1]] == (
            [1, 3] if case == "multi" else [1]
        )
        assert all(t["field_sources"]["nights"] == "proposal" for t in tasks[:-1])
        assert all(t["condition_strengths"]["nights"] == "soft" for t in tasks[:-1])
    assert not tasks[-1]["requires_hotel"]


@pytest.mark.parametrize("valid_repair", [True, False])
async def test_missing_stay_is_repaired_once_or_stops_before_queries(tmp_path, valid_repair):
    bad = proposal("normal")
    bad["tasks"][0]["conditions"]["nights"] = None
    bad["tasks"][0]["condition_strengths"].pop("nights")
    repaired = proposal("normal") if valid_repair else deepcopy(bad)
    harness, context, model, train, hotel = setup_run(tmp_path, bad, [repaired])
    if valid_repair:
        result = await harness.run_turn(context, RunState("repair"))
        assert result["status"] == "completed"
        assert train.calls and hotel.calls
    else:
        result = await harness.run_turn(context, RunState("repair"))
        assert result["status"] == "error"
        assert not train.calls and not hotel.calls
    assert model.stages == ["main:plan", "main:repair_proposal"]
