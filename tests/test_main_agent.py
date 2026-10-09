import json
from types import SimpleNamespace

import pytest


def weather_plan(**changes):
    return {"rewritten_query": "北京明天天气", "intents": [], "key_entities": {},
            "response_mode": "answer", "agent_schedule": [{"agent_name": "information_query",
            "priority": 2, "requested_domains": ["weather"]}], **changes}


@pytest.mark.asyncio
async def test_main_plan_only_advertises_business_agents():
    from agents.main_agent import MainAgent
    calls = []
    async def model(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(text=json.dumps(weather_plan()))
    agent = MainAgent(model)
    decision = await agent.plan({"original_query": "北京明天天气怎么样？"})
    assert decision["finalization_mode"] == "synthesize"
    assert "tools" not in calls[0][1]
    prompt = str(calls[0][0])
    assert "北京明天天气怎么样？" in prompt
    assert "rag_knowledge" in prompt
    assert "event_collection" not in prompt
    assert "train_search" not in prompt


@pytest.mark.parametrize("change", [
    {"agent_schedule": [{"agent_name": "train_search", "priority": 1}]},
    {"agent_schedule": [{"agent_name": "information_query", "priority": True}]},
    {"agent_schedule": [{"agent_name": "information_query", "priority": float('nan')}]},
    {"response_mode": "itinerary", "finalization_mode": "forward"},
    {"agent_schedule": [{"agent_name": "memory_query", "depends_on": ["rag_knowledge"]},
                        {"agent_name": "rag_knowledge", "depends_on": ["memory_query"]}]},
])
def test_invalid_plan_is_rejected(change):
    from agents.contracts import validate_plan
    with pytest.raises(ValueError):
        validate_plan(weather_plan(**change))


@pytest.mark.asyncio
async def test_main_finalize_has_no_query_tools():
    from agents.main_agent import MainAgent
    calls = []
    async def model(messages, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text='{"action":"answer","final_answer":"已查到天气"}')
    assert (await MainAgent(model).finalize({"original_query": "天气"}))["action"] == "answer"
    assert calls == [{}]


def test_run_state_does_not_share_mutable_results():
    from agents.contracts import RunState
    a, b = RunState("a"), RunState("b")
    a.domain_results["weather"] = {}
    assert b.domain_results == {}
