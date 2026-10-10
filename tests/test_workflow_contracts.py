from copy import deepcopy
import pytest
from agents.workflow_contracts import create_workflow, effective_conditions, validate_action, apply_user_update
from workflow_support import proposal, workflow


def test_defaults_are_not_confirmed_and_three_tasks_have_stable_dependencies():
    w = workflow()
    assert w['effective_conditions']['passengers'] == 1
    assert w['effective_conditions']['guests'] == 1
    assert w['field_sources']['passengers'] == 'default'
    assert w['field_sources']['guests'] == 'derived'
    assert w['effective_conditions']['constraints'] == {}
    assert w['confirmed_conditions'].get('passengers') is None
    assert [t['status'] for t in w['tasks']] == ['pending']*3
    assert w['tasks'][-1]['requires_hotel'] is False
    assert w['tasks'][1]['depends_on'] == [w['tasks'][0]['id']]


def test_conditions_merge_without_overwriting_input_or_forcing_brand():
    confirmed = {'passengers': 2, 'constraints': {'seat_class': '二等座'}}
    value, sources = effective_conditions(confirmed, {'passengers': 3}, {'hotel_brand': '如家'})
    assert value['passengers'] == value['guests'] == 3
    assert value['constraints'] == {'seat_class': '二等座'}
    assert sources['passengers'] == 'user'
    assert confirmed['passengers'] == 2


@pytest.mark.parametrize('count', [0, -1, True, '1'])
def test_invalid_counts_are_rejected(count):
    p = proposal()
    p['confirmed_conditions']['passengers'] = count
    with pytest.raises(ValueError): create_workflow(p, {})


def test_broken_route_and_invalid_dependency_are_rejected():
    for mutate in [lambda p: p['tasks'][1].update(origin='苏州'),
                   lambda p: p['tasks'][0].update(depends_on=[1]),
                   lambda p: p['tasks'][0].update(status='validated')]:
        p = proposal(); mutate(p)
        with pytest.raises(ValueError): create_workflow(p, {})


def test_task_two_update_preserves_first_and_invalidates_downstream():
    w = workflow()
    for t in w['tasks']: t.update(status='validated', draft_plan={'task_revision': 1})
    ids = [t['id'] for t in w['tasks']]
    updated = apply_user_update(w, {'task_updates': [{'task_id': ids[1], 'conditions': {'departure_date': '2026-10-14'}}]})
    assert [t['id'] for t in updated['tasks']] == ids
    assert updated['tasks'][0] == w['tasks'][0]
    assert [t['revision'] for t in updated['tasks']] == [1, 2, 2]
    assert [t['status'] for t in updated['tasks']] == ['validated', 'pending', 'pending']
    assert updated['tasks'][1]['draft_plan']['task_revision'] == 1
    assert updated['current_task_id'] == ids[1]


def test_global_party_change_is_confirmed_but_default_was_not():
    w = workflow()
    revised = apply_user_update(w, {'confirmed_conditions': {'passengers': 3}})
    assert revised['confirmed_conditions']['passengers'] == 3
    assert revised['effective_conditions']['guests'] == 3
    assert all(t['revision'] == 2 for t in revised['tasks'])
    assert w['confirmed_conditions'].get('passengers') is None


def test_stale_or_overreaching_actions_cannot_execute():
    w = workflow(); task = w['tasks'][0]
    with pytest.raises(ValueError):
        validate_action({'action': 'draft_task', 'task_id': task['id'], 'task_revision': 0, 'draft_plan': {}}, w)
    with pytest.raises(ValueError):
        validate_action({'action': 'dispatch', 'task_id': task['id'], 'goal': '天气',
                         'query_requests': [{'domain': 'weather', 'parameters': {}}]}, w)
    with pytest.raises(ValueError): validate_action({'action': 'finish', 'status': 'completed'}, w)
    with pytest.raises(ValueError): validate_action({'action': 'destroy'}, w)


def test_unknown_dates_do_not_silently_become_today():
    p = proposal(); p['confirmed_conditions'] = {}
    for t in p['tasks']: t['conditions'] = {}; t['field_sources'] = {}
    w = create_workflow(p, {'current_time': '2026-10-10T10:00:00+08:00'})
    assert w['confirmed_conditions'].get('start_date') is None
    assert w['tasks'][0]['conditions'].get('departure_date') is None


def test_first_departure_can_inherit_explicit_start_date():
    p = proposal(); p['tasks'][0]['conditions'].pop('departure_date')
    w = create_workflow(p, {})
    assert w['tasks'][0]['conditions']['departure_date'] == '2026-10-11'
    assert w['tasks'][0]['field_sources']['departure_date'] == 'derived'


def test_structured_workflow_proposal_cannot_fall_through_to_legacy_finalizer():
    from agents.contracts import validate_plan
    result = validate_plan({'response_mode': 'itinerary', 'finalization_mode': 'synthesize',
                            'agent_schedule': [], 'workflow_proposal': proposal()})
    assert result['response_mode'] == 'workflow'


def test_explicit_destination_update_changes_only_that_task_and_downstream_origin():
    w = workflow(); ids = [t['id'] for t in w['tasks']]
    revised = apply_user_update(w, {'task_updates': [{'task_id': ids[1], 'destination': '苏州'}]})
    assert revised['tasks'][0] == w['tasks'][0]
    assert revised['tasks'][1]['destination'] == revised['tasks'][2]['origin'] == '苏州'
    assert [t['id'] for t in revised['tasks']] == ids
    assert [t['revision'] for t in revised['tasks']] == [1, 2, 2]


def test_redundant_route_and_scope_metadata_is_checked_then_canonicalized():
    p = proposal()
    p['confirmed_conditions'].update(origin='上海', destinations=['北京', '杭州'], return_to='上海',
                                     constraints={'only_train_hotel': True, 'no_weather': True, 'no_guide': True, 'total_budget_cny': '1000'})
    w = create_workflow(p, {})
    assert 'origin' not in w['confirmed_conditions']
    assert w['confirmed_conditions']['constraints'] == {'total_budget_cny': '1000'}
    p['confirmed_conditions']['return_to'] = '苏州'
    with pytest.raises(ValueError): create_workflow(p, {})


def test_explicit_first_departure_update_revises_overall_start_consistently():
    w = workflow()
    revised = apply_user_update(w, {'task_updates': [{'task_id': w['tasks'][0]['id'], 'conditions': {'departure_date': '2026-10-12'}}]})
    assert revised['confirmed_conditions']['start_date'] == '2026-10-12'
    assert all(t['revision'] == 2 and t['status'] == 'pending' for t in revised['tasks'])
    assert [t['id'] for t in revised['tasks']] == [t['id'] for t in w['tasks']]
    with pytest.raises(ValueError):
        apply_user_update(w, {'confirmed_conditions': {'start_date': '2026-10-13'},
                             'task_updates': [{'task_id': w['tasks'][0]['id'], 'conditions': {'departure_date': '2026-10-12'}}]})
