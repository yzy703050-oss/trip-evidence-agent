"""Offline regression scenarios grouped by responsibility."""

import importlib
from types import SimpleNamespace

import pytest

from trip_assistant.llm.budget import BudgetedModel

# resource_calibration


# memory_budget_config


@pytest.mark.parametrize("budget", [20000, 14854])
def test_soft_trigger_is_eighty_percent_but_hard_check_remains_budget(budget):
    wrapped = BudgetedModel(None, budget_tokens=budget, counter=lambda payload: int(budget * 0.9))
    assert wrapped.compaction_trigger_tokens == budget * 80 // 100
    assert wrapped.check([]).tokens > wrapped.compaction_trigger_tokens


# context_budget


@pytest.mark.asyncio
async def test_budget_telemetry_failure_preserves_normal_model_reply():
    context_budget = importlib.import_module("trip_assistant.llm.budget")
    reply = SimpleNamespace(content="普通回复")

    async def model(messages, **kwargs):
        return reply

    def record(event):
        raise OSError("telemetry storage unavailable")

    wrapped = context_budget.BudgetedModel(model, budget_tokens=6000, record=record)
    assert await wrapped([{"role": "user", "content": "普通请求"}]) is reply


def test_input_count_includes_instructions_business_and_tools():
    context_budget = importlib.import_module("trip_assistant.llm.budget")
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "business facts"},
    ]
    tools = [{"type": "function", "function": {"name": "search", "parameters": {}}}]
    seen = []

    def counter(payload):
        seen.append(payload)
        return 37

    result = context_budget.count_input_tokens(messages, tools, counter=counter)
    assert result.tokens == 37
    assert not result.estimated
    assert seen == [{"messages": messages, "tools": tools}]


def test_offline_count_reports_estimate_method():
    context_budget = importlib.import_module("trip_assistant.llm.budget")
    result = context_budget.count_input_tokens([{"role": "user", "content": "北京"}])
    assert result.tokens > 0
    assert result.estimated
    assert result.method == "utf8_conservative"
