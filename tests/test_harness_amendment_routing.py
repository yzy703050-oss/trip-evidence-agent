"""Conflicting read/mutation decisions cannot silently skip the requested update."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.contracts import validate_plan
from agents.main_agent import MainAgent


def replacement():
    return {
        "intents": [{"type": "update"}],
        "agent_schedule": [],
        "response_mode": "workflow",
        "resume_workflow_id": "w1",
        "travel_update": {
            "update_type": "replace",
            "target": {"workflow_id": "w1", "task_ids": ["t1"], "components": ["hotel"]},
            "condition_updates": {},
        },
    }


@pytest.mark.parametrize(
    "mutation", ["travel_update", "workflow_update", "amendment_patch", "workflow_proposal"]
)
def test_read_and_amend_decision_is_rejected_before_effects(mutation):
    decision = replacement()
    if mutation != "travel_update":
        decision.pop("travel_update")
        decision[mutation] = {"target": "w1"}
    decision["read_request"] = {"workflow_id": "w1", "domain": "hotel"}
    with pytest.raises(ValueError, match="read.*mutat|read.*modif|只读"):
        validate_plan(decision)


@pytest.mark.asyncio
async def test_conflicting_model_decision_uses_one_repair():
    good = replacement()
    bad = {**deepcopy(good), "read_request": {"workflow_id": "w1", "domain": "hotel"}}
    seen = []

    async def model(messages, **kwargs):
        seen.append(messages)
        return SimpleNamespace(text=json.dumps(bad if len(seen) == 1 else good))

    decision = await MainAgent(model).initialize({"original_query": "把酒店换成门店12"})
    assert len(seen) == 2
    assert decision["travel_update"] == good["travel_update"]
    assert "read_request" not in decision


def test_pure_read_and_preference_preflight_remain_valid():
    read = {
        "response_mode": "direct",
        "final_answer": "读取保存资料",
        "agent_schedule": [],
        "read_request": {"workflow_id": "w1"},
    }
    assert validate_plan(read)["read_request"]["workflow_id"] == "w1"
    decision = replacement()
    decision["agent_schedule"] = [{"agent_name": "preference"}]
    assert validate_plan(decision)["travel_update"] == decision["travel_update"]
