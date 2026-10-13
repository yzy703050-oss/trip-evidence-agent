from copy import deepcopy

import pytest

from agents.workflow_contracts import create_workflow
from agents.workflow_queries import record_query
from test_workflow_queries import train_result


def dated_workflow(*, soft=False):
    return create_workflow({'tasks': [{
        'origin': '重庆', 'destination': '上海', 'requires_hotel': False,
        'conditions': {'departure_date': '2026-10-20',
                       'arrival_before': '2026-10-20T10:00:00+08:00'},
        'field_sources': {'departure_date': 'user', 'arrival_before': 'user'},
        'condition_strengths': {'departure_date': 'soft' if soft else 'hard',
                                'arrival_before': 'hard'}
    }]}, {})


def add_trains(w):
    task = w['tasks'][0]
    data = train_result(task)
    return record_query(w, task['id'], data['query'], {}, data, {}, domain='train')


def test_known_late_candidates_produce_sourced_hard_conflict():
    from agents.condition_conflicts import find_hard_date_conflict

    w = dated_workflow()
    row = add_trains(w)
    conflict = find_hard_date_conflict(w)
    assert conflict['code'] == 'hard_date_conflict'
    assert conflict['affected_task_ids'] == [w['tasks'][0]['id']]
    assert row['id'] in conflict['evidence_refs']
    assert '10:00' in conflict['question']


@pytest.mark.parametrize('case', ['alternative', 'unknown', 'failed', 'stale', 'soft', 'empty'])
def test_missing_evidence_or_possible_alternative_does_not_force_question(case):
    from agents.condition_conflicts import find_hard_date_conflict

    w = dated_workflow(soft=case == 'soft')
    row = add_trains(w)
    if case == 'alternative':
        row['items'][-1]['arrival_at'] = '2026-10-20T09:00:00+08:00'
        row['items'][-1]['arrival_time'] = '09:00'
    elif case == 'unknown':
        row['items'][-1]['arrival_at'] = None
    elif case == 'failed':
        row['needs_revalidation'] = True
    elif case == 'stale':
        w['tasks'][0]['revision'] += 1
    elif case == 'empty':
        row['items'] = []
    assert find_hard_date_conflict(w) is None


def test_contradictory_hard_dates_need_no_supplier_evidence():
    from agents.condition_conflicts import find_hard_date_conflict

    w = dated_workflow()
    w['tasks'][0]['conditions']['departure_date'] = '2026-10-21'
    assert find_hard_date_conflict(w)['code'] == 'hard_date_conflict'


@pytest.mark.asyncio
async def test_runner_saves_wait_and_resumes_explicit_changed_deadline(tmp_path):
    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor
    from workflow_runtime import WorkflowMain, OfflineProvider

    w = dated_workflow()
    w['tasks'][0]['conditions']['departure_date'] = '2026-10-21'
    p = {'tasks': [{k: deepcopy(v) for k, v in w['tasks'][0].items()
                   if k in {'origin', 'destination', 'requires_hotel', 'conditions',
                            'field_sources', 'condition_strengths'}}]}
    main = WorkflowMain(p)
    memory = MemoryManager('a', 'hard-conflict', str(tmp_path))
    harness = ExecutionHarness(main, {}, memory)
    train = OfflineProvider('train')
    harness.runner.executor = ToolExecutor({'train_search': train})
    context = {'original_query': '20日出发，必须10点前到上海，只安排单程',
               'current_time': '2026-10-13T10:00:00+08:00'}
    result = await harness.run_turn(context, RunState('first'))
    assert result['status'] == 'needs_input'
    saved = memory.workflow_store.load(result['workflow_id'])
    assert saved['status'] == 'running' and saved['stop_reason'] == 'user_input_required'
    assert saved['checkpoint']['kind'] == 'hard_condition_conflict'
    assert len(train.calls) == 0 and main.actions == []
    main.resume = {'id': saved['id'], 'update': {'task_updates': [{
        'task_id': saved['tasks'][0]['id'],
        'conditions': {'departure_date': '2026-10-20', 'arrival_before': '2026-10-20T13:00:00+08:00'}
    }]}}
    result = await harness.run_turn({**context, 'original_query': '改成13点前到达'}, RunState('second'))
    assert result['status'] == 'completed'
    assert result['workflow_id'] == saved['id']
