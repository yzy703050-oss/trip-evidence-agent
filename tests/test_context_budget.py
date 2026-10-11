import importlib


def test_input_count_includes_instructions_business_and_tools():
    context_budget = importlib.import_module("context.context_budget")
    messages = [{"role": "system", "content": "instructions"},
                {"role": "user", "content": "business facts"}]
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
    context_budget = importlib.import_module("context.context_budget")
    result = context_budget.count_input_tokens([{"role": "user", "content": "北京"}])
    assert result.tokens > 0
    assert result.estimated
    assert result.method == "utf8_conservative"
