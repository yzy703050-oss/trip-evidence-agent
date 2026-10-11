from copy import deepcopy

import pytest
from test_preflight_workflow import runtime

from agents.planning_conditions import prepare_task
from agents.workflow_contracts import create_workflow


def proposal():
    return {'confirmed_conditions': {}, 'tasks': [
        dict(origin=None, destination='上海', requires_hotel=True,
             conditions={'departure_date': '2026-10-12', 'nights': 3},
             field_sources={'departure_date': 'user', 'nights': 'proposal'})]}


def context(query='我明天去上海玩'):
    return dict(original_query=query, default_round_trip=True,
                effective_preferences={'home_location': '重庆'})


def test_new_trip_defaults_return_home_without_filling_stay_length():
    p = proposal()
    before = deepcopy(p)
    w = create_workflow(p, context(), require_stay_proposals=True)
    assert [(t['origin'], t['destination']) for t in w['tasks']] == [
        ('重庆', '上海'), ('上海', '重庆')]
    assert w['tasks'][0]['conditions']['nights'] == 3
    assert w['tasks'][-1]['purpose'] == 'return'
    assert not w['tasks'][-1]['requires_hotel']
    assert 'nights' not in w['tasks'][-1]['conditions']
    assert p == before


@pytest.mark.parametrize('query', [
    '只安排单程去上海', '去上海，不用安排回程', '去上海，暂时不回重庆'])
def test_explicit_one_way_is_respected(query):
    w = create_workflow(proposal(), context(query), require_stay_proposals=True)
    assert len(w['tasks']) == 1


def test_multiple_destinations_return_once_to_home_not_temporary_origin():
    p = proposal()
    p['tasks'][0]['origin'] = '北京'
    p['tasks'].append(dict(origin='上海', destination='杭州', requires_hotel=True,
                          conditions={'nights': 1}, field_sources={'nights': 'proposal'}))
    w = create_workflow(p, context('从北京去上海，然后杭州'), require_stay_proposals=True)
    assert [(t['origin'], t['destination']) for t in w['tasks']] == [
        ('北京', '上海'), ('上海', '杭州'), ('杭州', '重庆')]
    closed = deepcopy(p)
    closed['tasks'].append(dict(origin='杭州', destination='重庆', purpose='return',
                                requires_hotel=False))
    assert len(create_workflow(closed, context(), require_stay_proposals=True)['tasks']) == 3


def test_explicit_return_destination_is_not_overridden_by_home():
    p = proposal()
    p['tasks'].append(dict(origin='上海', destination='北京', purpose='return',
                          purpose_source='user', requires_hotel=False))
    w = create_workflow(p, context('去上海玩，最后回北京'), require_stay_proposals=True)
    assert len(w['tasks']) == 2 and w['tasks'][-1]['destination'] == '北京'


def test_declining_same_day_return_is_not_declining_return():
    w = create_workflow(proposal(), context('去上海住三晚，不要当天返程'),
                        require_stay_proposals=True)
    assert len(w['tasks']) == 2


def test_return_can_query_suggested_checkout_even_if_outgoing_train_failed():
    w = create_workflow(proposal(), context(), require_stay_proposals=True)
    t = w['tasks'][-1]
    prepared = prepare_task(w, t['id'], '2026-10-11T10:00:00+08:00',
                            previous_boundary={'check_out': '2026-10-15', 'arrival_at': None})
    assert prepared['query_requests'][0]['parameters']['departure_date'] == '2026-10-15'
    assert t['field_sources']['departure_date'] == 'proposal'


@pytest.mark.asyncio
async def test_actual_harness_default_round_trip_queries_outgoing_and_return(tmp_path):
    from agents.contracts import RunState
    r = runtime(tmp_path, proposal()['tasks'])
    r.memory.long_term.set_user_preference('home_location', '重庆')
    run = RunState(r.memory.start_turn('去上海玩'), effective_preferences={'home_location': '重庆'})
    result = await r.harness.run_turn({'original_query': '去上海玩',
        'current_time': '2026-10-11T10:00:00+08:00'}, run)
    assert result['status'] == 'completed'
    assert len(result['workflow']['tasks']) == 2
    assert len(r.train.calls) == 2 and len(r.hotel.calls) == 1
    assert r.train.calls[1].isoformat() == result['workflow']['tasks'][0]['conditions']['check_out']


def test_return_date_tracks_updated_stay_even_without_arrival_evidence():
    w = create_workflow(proposal(), context(), require_stay_proposals=True)
    t = w['tasks'][-1]
    t['conditions']['departure_date'] = '2026-10-15'
    t['field_sources']['departure_date'] = 'proposal'
    t['needs_dependency_check'] = True
    prepared = prepare_task(w, t['id'], '2026-10-11T10:00:00+08:00',
                            previous_boundary={'check_out': '2026-10-16', 'arrival_at': None})
    assert prepared['query_requests'][0]['parameters']['departure_date'] == '2026-10-16'


def test_explicit_return_date_is_preserved_even_after_dependency_change():
    w = create_workflow(proposal(), context(), require_stay_proposals=True)
    t = w['tasks'][-1]
    t['conditions']['departure_date'] = '2026-10-18'
    t['field_sources']['departure_date'] = 'user'
    t['needs_dependency_check'] = True
    prepared = prepare_task(w, t['id'], '2026-10-11T10:00:00+08:00',
                            previous_boundary={'check_out': '2026-10-16',
                                               'arrival_at': '2026-10-13T09:00:00+08:00'})
    assert prepared['query_requests'][0]['parameters']['departure_date'] == '2026-10-18'


@pytest.mark.asyncio
async def test_overnight_arrival_moves_stay_and_return_day(tmp_path):
    p = proposal()
    p['tasks'][0]['origin'] = '重庆'
    r = runtime(tmp_path, p['tasks'])
    r.train.duration_minutes = 1500
    result = await r.turn('从重庆去上海玩')
    outgoing, returning = result['workflow']['tasks']
    assert result['status'] == 'completed'
    assert outgoing['conditions']['check_in'] == '2026-10-13'
    assert outgoing['conditions']['check_out'] == '2026-10-16'
    assert returning['conditions']['departure_date'] == '2026-10-16'
    assert r.train.calls[-1].isoformat() == '2026-10-16'


@pytest.mark.asyncio
async def test_failed_outgoing_train_still_queries_return_from_proposed_stay(tmp_path):
    from dataclasses import replace

    from travel_data.tools import ToolExecutor
    p = proposal()
    p['tasks'][0]['origin'] = '重庆'
    p['tasks'][0]['conditions'].update(check_in='2026-10-12', check_out='2026-10-15')
    p['tasks'][0]['field_sources'].update(check_in='proposal', check_out='proposal')
    r = runtime(tmp_path, p['tasks'])
    provider = r.train
    class FailsOutgoing:
        async def search(self, query):
            response = await provider.search(query)
            if query.origin == '重庆':
                return replace(response, status='error', items=[], message='timeout')
            return response
    r.info.tool_executor = ToolExecutor({'train_search': FailsOutgoing(), 'hotel_search': r.hotel})
    result = await r.turn('从重庆去上海玩')
    assert result['status'] == 'partial'
    assert [day.isoformat() for day in provider.calls] == ['2026-10-12', '2026-10-15']
    assert result['validated_plan']['reconstructed_tasks'][0]['train'] is None
    assert result['validated_plan']['reconstructed_tasks'][1]['train']
    for phrase in ['没查到', '计划2026年10月12日出发', '建议住3晚',
                   '2026年10月15日', '上海到重庆', '售票渠道']:
        assert phrase in result['final_answer']
