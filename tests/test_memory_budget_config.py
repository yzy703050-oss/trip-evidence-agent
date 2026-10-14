import pytest

from config import LLM_CONFIG, Settings, get_model_generate_kwargs
from context.context_budget import BudgetedModel


def test_default_and_explicit_budget_preserve_temperature(monkeypatch):
    monkeypatch.delenv("MEMORY_CONTEXT_BUDGET_TOKENS", raising=False)
    assert Settings(_env_file=None).memory_context_budget_tokens == 20000
    assert (
        Settings(_env_file=None, memory_context_budget_tokens=12345).memory_context_budget_tokens
        == 12345
    )
    assert LLM_CONFIG["temperature"] == 0.3
    assert get_model_generate_kwargs({})["temperature"] == 0.3


@pytest.mark.parametrize("budget", [20000, 14854, 12345])
def test_soft_trigger_is_eighty_percent_but_hard_check_remains_budget(budget):
    wrapped = BudgetedModel(None, budget_tokens=budget, counter=lambda payload: int(budget * 0.9))
    assert wrapped.compaction_trigger_tokens == budget * 80 // 100
    assert wrapped.check([]).tokens > wrapped.compaction_trigger_tokens
