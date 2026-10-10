from copy import deepcopy

import pytest

from agents.workflow_contracts import create_workflow, validate_action


def skeleton(origin='重庆', *, arrival=None):
    conditions = {'arrival_date': arrival} if arrival else {}
    return create_workflow({'confirmed_conditions': {}, 'tasks': [
        {'origin': origin, 'destination': '上海', 'purpose': 'visit',
         'purpose_source': 'user', 'requires_hotel': True,
         'conditions': conditions, 'field_sources': {'arrival_date': 'user'} if arrival else {}}
    ]}, {'original_query': '我要去上海玩'})


def test_route_skeleton_allows_unknown_origin_but_preserves_user_purpose():
    w = skeleton(None)
    assert w['tasks'][0]['origin'] is None
    assert w['tasks'][0]['purpose'] == 'visit'
    assert w['tasks'][0]['status'] == 'pending'


def test_default_week_later_is_not_confirmed_and_does_not_drift():
    from agents.planning_conditions import prepare_task
    w = skeleton()
    t = w['tasks'][0]
    first = prepare_task(w, t['id'], '2026-10-10T23:00:00+08:00')
    assert first['effective_conditions']['departure_date'] == '2026-10-17'
    assert first['field_sources']['departure_date'] == 'default'
    assert 'start_date' not in w['confirmed_conditions']
    later = prepare_task(w, t['id'], '2026-10-11T08:00:00+08:00')
    assert later['effective_conditions']['departure_date'] == '2026-10-17'


def test_default_is_based_on_beijing_calendar_not_utc():
    from agents.planning_conditions import prepare_task
    w = skeleton()
    result = prepare_task(w, w['tasks'][0]['id'], '2026-10-10T20:00:00+00:00')
    assert result['effective_conditions']['departure_date'] == '2026-10-18'


def test_arrival_requirement_is_preserved_without_default_departure():
    from agents.planning_conditions import prepare_task
    w = skeleton(arrival='2026-10-18')
    result = prepare_task(w, w['tasks'][0]['id'], '2026-10-10T08:00:00+08:00')
    assert result['effective_conditions']['arrival_date'] == '2026-10-18'
    assert result['effective_conditions'].get('departure_date') is None
    assert result['missing_fields'] == []
    assert result['query_mode'] == 'arrival'


def test_missing_origin_does_not_prevent_independent_hotel_query():
    from agents.planning_conditions import prepare_task
    w = skeleton(None)
    result = prepare_task(w, w['tasks'][0]['id'], '2026-10-10T08:00:00+08:00')
    assert result['missing_fields'] == ['origin']
    assert result['query_requests'] == [{'domain': 'hotel', 'parameters': {'city': '上海', 'guests': 1}}]


def test_return_with_explicit_hotel_requirement_is_allowed():
    p = {'confirmed_conditions': {}, 'tasks': [
        dict(origin='上海', destination='北京', purpose='visit', requires_hotel=True),
        dict(origin='北京', destination='上海', purpose='return', purpose_source='user', requires_hotel=True)
    ]}
    w = create_workflow(p, {})
    assert w['tasks'][-1]['purpose'] == 'return'
    assert w['tasks'][-1]['requires_hotel'] is True


def test_second_leg_uses_reliable_previous_boundary_instead_of_week_default():
    from agents.planning_conditions import prepare_task
    p = {'confirmed_conditions': {}, 'tasks': [
        dict(origin='上海', destination='北京', requires_hotel=False),
        dict(origin='北京', destination='杭州', requires_hotel=False)]}
    w = create_workflow(p, {})
    result = prepare_task(w, w['tasks'][1]['id'], '2026-10-10T08:00:00+08:00',
                          previous_boundary={'arrival_at': '2026-10-19T09:00:00+08:00'})
    assert result['effective_conditions']['departure_date'] == '2026-10-19'
    assert result['field_sources']['departure_date'] == 'derived'
    assert result['query_requests'][0]['parameters']['constraints']['departure_time_after'] == '09:00'


def test_dispatch_completion_mode_uses_existing_task_identity():
    w = skeleton()
    t = w['tasks'][0]
    action = validate_action(dict(action='dispatch', mode='complete_conditions', task_id=t['id'],
                                  task_revision=1, goal='补全', query_requests=[]), w)
    assert action['mode'] == 'complete_conditions'
    with pytest.raises(ValueError):
        validate_action({**action, 'mode': 'invent_conditions'}, w)


def test_invalid_known_route_still_rejected():
    p = {'confirmed_conditions': {}, 'tasks': [dict(origin='上海', destination='北京', requires_hotel=False),
                                             dict(origin='杭州', destination='上海', requires_hotel=False)]}
    with pytest.raises(ValueError):
        create_workflow(deepcopy(p), {})
