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








def test_missing_conditions_cannot_complete_itinerary():
    result = guard_final_itinerary({'action': 'itinerary', 'planning_complete': True,
        'itinerary': {'daily_plans': [{'day': 1}]}}, {})
    assert result['planning_complete'] is False
    assert result['missing_fields']
