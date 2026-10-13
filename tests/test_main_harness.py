import json
from copy import deepcopy

import pytest
from agents.runtime import Msg
from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness


def weather_info():
    source = {'provider': 'weather', 'fetched_at': '2026-10-09T09:00:00+08:00', 'url': 'https://example.com/weather'}
    return {'status': 'ok', 'summary': '北京明天多云，18～25℃', 'missing_fields': [], 'travel_conditions': {},
        'domain_results': {'weather': {'status': 'ok', 'query': {'city': '北京', 'date': '2026-10-10'},
        'items': [{'date': '2026-10-10', 'description': '多云', 'min_temp_c': 18, 'max_temp_c': 25, 'source': source}],
        'source': source, 'missing_fields': [], 'fetched_at': source['fetched_at'], 'message': None}}}


class Main:
    def __init__(self, schedule=None, mode='forward', response_mode='answer'):
        self.schedule = schedule or [{'agent_name': 'information_query', 'priority': 2, 'requested_domains': ['weather']}]
        self.mode, self.response_mode = mode, response_mode
        self.finalize_calls = 0
        self.final_context = None

    async def plan(self, context):
        return {'response_mode': self.response_mode, 'finalization_mode': self.mode,
                'rewritten_query': context['original_query'], 'agent_schedule': self.schedule}

    async def finalize(self, context):
        self.finalize_calls += 1
        self.final_context = context
        return {'action': 'answer', 'final_answer': '综合回答'}


class Info:
    def __init__(self, value=None):
        self.value = value or weather_info()
        self.context = None
        self.executions = 0

    async def run(self, context, run):
        self.executions += 1
        self.context = deepcopy(context)
        run.domain_results.update(self.value['domain_results'])
        return deepcopy(self.value)
