from copy import deepcopy
import pytest
from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from agents.itinerary_module import guard_final_itinerary
from test_main_harness import Main, Info


class FeedbackMain(Main):
    async def finalize(self, context):
        self.finalize_calls += 1
        return {'action': 'needs_requery', 'reason': '比较下一批天气来源',
                'domains': ['weather'], 'constraints': {'candidate_offset': 5}}


@pytest.mark.asyncio
async def test_feedback_limit_stops_second_requery():
    main, info, run = FeedbackMain(mode='synthesize'), Info(), RunState('a')
    result = await ExecutionHarness(main, {'information_query': info}).run_turn({'original_query': '天气'}, run)
    assert info.executions == main.finalize_calls == 2
    assert info.context['feedback']['domains'] == ['weather']
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'feedback_limit'
    assert result['domain_results']['weather']['items']


@pytest.mark.asyncio
async def test_feedback_preserves_hard_constraints():
    main, info = FeedbackMain(mode='synthesize'), Info()
    run = RunState('a', travel_conditions={'constraints': {'candidate_offset': 0}})
    result = await ExecutionHarness(main, {'information_query': info}).run_turn({'original_query': '天气'}, run)
    assert info.executions == 1
    assert result['stop_reason'] == 'invalid_feedback'
    assert run.travel_conditions['constraints']['candidate_offset'] == 0


@pytest.mark.asyncio
async def test_feedback_only_requeries_affected_domain():
    from agentscope.message import Msg
    class Other:
        def __init__(self): self.executions = 0
        async def reply(self, msg):
            self.executions += 1
            return Msg('other', '{}', 'assistant')
    schedule = [{'agent_name': n, 'priority': 1} for n in ('preference', 'memory_query', 'rag_knowledge')]
    schedule.append({'agent_name': 'information_query', 'requested_domains': ['weather', 'web']})
    main, info = FeedbackMain(schedule, mode='synthesize'), Info()
    others = {name: Other() for name in ('preference', 'memory_query', 'rag_knowledge')}
    await ExecutionHarness(main, {**others, 'information_query': info}).run_turn({'original_query': '综合查询'}, RunState('a'))
    assert all(o.executions == 1 for o in others.values())
    assert info.context['requested_domains'] == ['weather']


def test_missing_conditions_cannot_complete_itinerary():
    result = guard_final_itinerary({'action': 'itinerary', 'planning_complete': True,
        'itinerary': {'daily_plans': [{'day': 1}]}}, {})
    assert result['planning_complete'] is False
    assert result['missing_fields']
