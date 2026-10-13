import json
from types import SimpleNamespace
import pytest
from agents.main_agent import MainAgent
from agents.execution_harness import ExecutionHarness
from agents.contracts import RunState
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.tools import ToolExecutor
from test_information_agent_loop import info_class, Public


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,query,answer', [
    ('ask', '解释为什么选这家酒店，不要重新查询', '依据已有酒店位置选择'),
    ('control', '帮我买这张火车票', '当前只能提供建议，无法执行购票'),
])
async def test_four_purpose_direct_decisions_do_not_query(kind, query, answer):
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text=json.dumps(dict(response_mode='direct', agent_schedule=[],
            intents=[{'type': kind}], final_answer=answer), ensure_ascii=False))
    run = RunState('direct')
    result = await ExecutionHarness(main_agent=MainAgent(model), agent_registry={}).run_turn(
        {'original_query': query}, run)
    assert result['status'] == 'ok' and result['final_answer'] == answer
    assert len(calls) == 1 and run.external_request_count == 0
    assert not run.tool_requests and 'workflow' not in result
