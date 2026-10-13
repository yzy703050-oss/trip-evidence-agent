from copy import deepcopy

import pytest

from agents.workflow_contracts import create_workflow, validate_action
from agents.workflow_repair import apply_repair
from agents.workflow_guard import check_workflow
from workflow_support import populated_workflow


def repairable():
    w = populated_workflow(quoted=True)
    w['condition_strengths'] = {'start_date': 'soft'}
    t = w['tasks'][0]
    t['condition_strengths'] = {'departure_date': 'soft', 'check_in': 'soft', 'check_out': 'soft'}
    t['draft_plan']['train_selection'] = None
    w['validation'] = check_workflow(w)
    return w


def date_action(w):
    t = w['tasks'][0]
    return {'action': 'repair_tasks', 'workflow_revision': w['revision'],
            'task_ids': [t['id']], 'components': ['schedule'],
            'conflict_codes': ['train_selection_missing'], 'reason': '协调新的出发与住宿日期',
            'task_updates': [{'task_id': t['id'], 'conditions': {
                'departure_date': '2026-10-12', 'check_in': '2026-10-12', 'check_out': '2026-10-14'}}]}


def test_model_patch_can_change_user_soft_dates_without_forging_old_quotes():
    w = repairable()
    before = deepcopy(w)
    action = date_action(w)
    assert validate_action(action, w)['task_updates'] == action['task_updates']
    updated = apply_repair(w, action)
    t = updated['tasks'][0]
    assert t['conditions']['departure_date'] == '2026-10-12'
    assert t['conditions']['check_out'] == '2026-10-14'
    assert t['field_sources']['departure_date'] == 'proposal'
    assert t['condition_strengths']['departure_date'] == 'soft'
    assert t['draft_plan']['hotel_selection'] is None
    assert updated['results_by_query'] == before['results_by_query']
    assert w == before


@pytest.mark.parametrize('case', ['hard', 'hard_same_value', 'global_end', 'hard_arrival',
                                 'hard_nights', 'unrelated', 'budget', 'source'])
def test_date_repair_rejects_changes_outside_permission(case):
    w = repairable()
    action = date_action(w)
    if case in {'hard', 'hard_same_value'}:
        w['tasks'][0]['condition_strengths']['departure_date'] = 'hard'
        if case == 'hard_same_value':
            action['task_updates'][0]['conditions']['departure_date'] = w['tasks'][0]['conditions']['departure_date']
    elif case == 'global_end':
        w['confirmed_conditions']['end_date'] = '2026-10-11'
    elif case == 'hard_arrival':
        w['tasks'][0]['conditions']['arrival_date'] = '2026-10-11'
        w['tasks'][0]['condition_strengths']['arrival_date'] = 'hard'
    elif case == 'hard_nights':
        w['tasks'][0]['conditions']['nights'] = 2
        w['tasks'][0]['condition_strengths']['nights'] = 'hard'
        action['task_updates'][0]['conditions']['check_out'] = '2026-10-15'
    elif case == 'unrelated':
        action['task_updates'][0]['task_id'] = w['tasks'][2]['id']
    elif case == 'budget':
        action['task_updates'][0]['conditions']['constraints'] = {'hotel_max_nightly_cny': 900}
    else:
        action['task_updates'][0]['condition_strengths'] = {'departure_date': 'soft'}
    before = deepcopy(w)
    with pytest.raises(ValueError):
        apply_repair(w, action)
    assert w == before


@pytest.mark.asyncio
async def test_empty_date_tool_does_not_advance_dates_itself(tmp_path):
    from test_preflight_workflow import runtime
    from datetime import date

    r = runtime(tmp_path, [{'origin': '重庆', 'destination': '上海',
                           'requires_hotel': False, 'conditions': {}}])
    r.train.empty_dates = {date(2026, 10, 17)}
    result = await r.turn('去上海，只安排单程火车')
    assert [d.isoformat() for d in r.train.calls] == ['2026-10-17']
    assert result['status'] != 'completed'


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_first', [False, True])
async def test_model_adjusts_outbound_and_return_and_queries_only_new_dates(tmp_path, invalid_first):
    from dataclasses import replace
    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor
    from workflow_runtime import WorkflowMain, OfflineProvider

    p = {'tasks': [dict(origin=a, destination=b, requires_hotel=False,
                       conditions={'departure_date': day}, field_sources={'departure_date': 'user'},
                       condition_strengths={'departure_date': 'soft'})
                  for a, b, day in [('重庆', '上海', '2026-10-17'), ('上海', '重庆', '2026-10-19')]]}

    class Main(WorkflowMain):
        async def select_batch(self, c):
            if not self.actions or (invalid_first and self.actions == ['invalid_repair']):
                w = c['workflow']
                assert any(i['code'] == 'selection_not_ready' for i in c['issues'])
                if self.actions:
                    assert c['action_feedback']['code'] == 'invalid_repair'
                invalid = invalid_first and not self.actions
                self.actions.append('invalid_repair' if invalid else 'repair_tasks')
                return {'action': 'repair_tasks', 'workflow_revision': w['revision'],
                        'task_ids': [t['id'] for t in w['tasks']], 'components': ['schedule'],
                        'conflict_codes': ['invented_empty_train'] if invalid else ['selection_not_ready'],
                        'reason': '去返程同时推迟一天，保持两天停留',
                        'task_updates': [{'task_id': t['id'], 'conditions': {'departure_date': d}}
                                         for t, d in zip(w['tasks'], ['2026-10-18', '2026-10-20'])]}
            return await super().select_batch(c)

    class Train(OfflineProvider):
        async def search(self, q):
            result = await super().search(q)
            return replace(result, items=[]) if str(q.departure_date) == '2026-10-17' else result

    train, main = Train('train'), Main(p)
    harness = ExecutionHarness(main, {}, MemoryManager('a', 'dates', str(tmp_path)))
    harness.runner.executor = ToolExecutor({'train_search': train})
    result = await harness.run_turn({'original_query': '去上海往返，日期可调整',
                                    'current_time': '2026-10-13T10:00:00+08:00'}, RunState('dates'))
    assert result['status'] == 'completed', result
    assert [q['departure_date'] for q in train.calls] == ['2026-10-17', '2026-10-19',
                                                       '2026-10-18', '2026-10-20']
    assert [t['conditions']['departure_date'] for t in result['workflow']['tasks']] == [
        '2026-10-18', '2026-10-20']
