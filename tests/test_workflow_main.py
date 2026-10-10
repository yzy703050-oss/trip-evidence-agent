import json
from types import SimpleNamespace
import pytest
from agents.main_agent import MainAgent
from agents.workflow_contracts import create_workflow
from workflow_support import proposal


@pytest.mark.asyncio
async def test_initialize_emits_proposal_without_duplicate_intent_call():
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text=json.dumps({'response_mode': 'workflow', 'finalization_mode': 'synthesize',
                                                'agent_schedule': [], 'workflow_proposal': proposal()}))
    main = MainAgent(model)
    decision = await main.initialize({'original_query': '上海北京杭州上海，只安排火车酒店'})
    assert decision['response_mode'] == 'workflow' and len(calls) == 1
    assert 'workflow_proposal' in calls[0][0]['content']


@pytest.mark.asyncio
async def test_step_has_full_task_overview_and_no_activities_guide():
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        return SimpleNamespace(text='{"action":"finish","status":"partial","final_answer":"资料不足","gaps":[]}')
    main = MainAgent(model); w = create_workflow(proposal(), {})
    await main.step({'workflow': w, 'current_task': w['tasks'][0], 'workflow_overview': w['tasks']})
    prompt = calls[0][0]['content']
    assert all(t['id'] in prompt for t in w['tasks'])
    assert 'suggestion:city_walk' not in prompt
    assert 'draft_task' in prompt and 'validate_workflow' in prompt
