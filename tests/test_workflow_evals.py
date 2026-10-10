from copy import deepcopy
import pytest
from evals.main_agent.multi_destination_runner import evaluate_result, offline_coverage
from agents.workflow_guard import check_workflow, finalize_workflow
from workflow_support import populated_workflow


def envelope():
    w = populated_workflow(); checked = check_workflow(w); w = finalize_workflow(w, checked)
    return dict(status='completed', workflow=w, validated_plan=checked, stop_reason='validated', final_answer='安排已整理')


def test_evaluator_requires_current_global_validation():
    valid = envelope()
    assert evaluate_result(valid, expected_tasks=3)['passed'] is True
    for mutate in [lambda r: r['workflow']['tasks'][0].update(status='draft'),
                   lambda r: r['workflow']['tasks'][0].update(revision=2),
                   lambda r: r['workflow'].update(validation=None),
                   lambda r: r['workflow']['tasks'][0]['draft_plan'].update(activities=[]),
                   lambda r: next(q for q in r['workflow']['results_by_query'].values() if q['domain']=='hotel')['items'][0].update(stay_total_cny='999')]:
        bad = deepcopy(valid); mutate(bad)
        assert evaluate_result(bad, expected_tasks=3)['passed'] is False


def test_needs_input_is_a_pause_not_a_query_success():
    value = envelope(); value['status'] = 'needs_input'; value['workflow']['status'] = 'needs_input'
    value['workflow']['checkpoint'] = {'question': '哪天出发？'}
    checked = evaluate_result(value, expected_tasks=3)
    assert checked['passed'] is True
    assert checked['planning_complete'] is False
    assert checked['external_query_verified'] is False


def test_every_acceptance_scenario_has_an_executable_test():
    coverage = offline_coverage()
    assert set(coverage) == {f'S{i:02}' for i in range(1, 23)}
    assert all(nodes for nodes in coverage.values())


def test_query_success_and_planning_completion_are_measured_separately():
    value = envelope(); value['status'] = 'needs_input'
    value['workflow']['checkpoint'] = {'question': '请确认日期'}
    row = {'status': 'ok', 'items': [{'id': 'sourced'}], 'source': {'provider': 'fixture'}}
    value['results'] = [{'data': {'query_results': [row]}}]
    result = evaluate_result(value, expected_tasks=3)
    assert result['external_query_verified'] is True
    assert result['planning_complete'] is False
    assert evaluate_result({'status': 'ok', 'domain_results': {'train': row}})['external_query_verified'] is True
