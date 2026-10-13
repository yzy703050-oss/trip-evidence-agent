from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from test_cli_session_resume import app_for
from test_preflight_workflow import runtime

from agents.workflow_contracts import apply_user_update, create_workflow
from context.memory_manager import MemoryManager


def route(origin=None, destination='上海'):
    return dict(confirmed_conditions={}, tasks=[dict(origin=origin, destination=destination,
                requires_hotel=True, conditions={'nights': 2})])


def test_home_is_default_first_origin_with_preference_evidence():
    proposal = route()
    original = deepcopy(proposal)
    workflow = create_workflow(proposal, {'effective_preferences': {'home_location': '重庆'}})
    assert workflow['tasks'][0]['origin'] == '重庆'
    assert workflow['tasks'][0]['field_sources']['origin'] == 'preference'
    assert proposal == original


def test_explicit_origin_and_subsequent_routes_take_priority():
    proposal = route('北京')
    proposal['tasks'].append(dict(origin=None, destination='杭州', requires_hotel=False))
    workflow = create_workflow(proposal, {'effective_preferences': {'home_location': '重庆'}})
    assert [task['origin'] for task in workflow['tasks']] == ['北京', '上海']


@pytest.mark.parametrize('home', [None, '', '上海'])
def test_unknown_or_same_city_home_does_not_create_invalid_train_route(home):
    workflow = create_workflow(route(), {'effective_preferences': {'home_location': home}})
    assert workflow['tasks'][0]['origin'] is None


@pytest.mark.asyncio
async def test_cli_uses_saved_home_after_reopening_without_origin_question(tmp_path):
    # Persist onboarding first, then use a fresh memory binding like a restart.
    onboarding = MemoryManager('alice', 'onboarding', str(tmp_path))
    onboarding.long_term.set_user_preference('home_location', '重庆')
    app = app_for(tmp_path)
    r = runtime(tmp_path, route()['tasks'], session='current')
    initial = r.main.initialize
    expected_date = None
    async def initialize(context):
        nonlocal expected_date
        # Stand in for the main model's relative-date extraction; exercise the
        # real CLI preference injection, harness, runner and query executor.
        today = datetime.fromisoformat(context['current_time']).date()
        expected_date = (today + timedelta(days=1)).isoformat()
        r.main.p['confirmed_conditions']['start_date'] = expected_date
        return await initial(context)
    r.main.initialize = initialize
    app.harness = r.harness
    assert await app.process_query('我明天想去上海玩')
    result = app.last_result
    assert result['status'] == 'completed'
    assert result['workflow']['tasks'][0]['origin'] == '重庆'
    assert r.train.calls[0].isoformat() == expected_date
    parameters = next(q['parameters'] for q in result['workflow']['results_by_query'].values() if q['domain'] == 'train')
    assert parameters['departure_date'] == expected_date
    assert len(r.train.calls) == 2 and len(r.hotel.calls) == 1
    assert result['workflow']['tasks'][-1]['destination'] == '重庆'
    assert '你准备从哪个城市出发' not in result['final_answer']
    assert '草稿包含' not in result['final_answer']
    assert app.memory_manager.long_term.get_preference('home_location') == '重庆'


@pytest.mark.asyncio
async def test_explicit_trip_origin_does_not_change_saved_home(tmp_path):
    app = app_for(tmp_path)
    app.memory_manager.long_term.set_user_preference('home_location', '重庆')
    r = runtime(tmp_path, route('北京')['tasks'], session='current')
    app.harness = r.harness
    assert await app.process_query('我这次从北京去上海玩')
    assert app.last_result['workflow']['tasks'][0]['origin'] == '北京'
    assert app.memory_manager.long_term.get_preference('home_location') == '重庆'


@pytest.mark.parametrize('legacy', [False, True])
def test_changing_default_origin_records_user_source(legacy):
    from agents.travel_updates import apply_travel_update
    workflow = create_workflow(route(), {'effective_preferences': {'home_location': '重庆'}})
    task_id = workflow['tasks'][0]['id']
    if legacy:
        updated = apply_user_update(workflow, {
            'task_updates': [{'task_id': task_id, 'origin': '北京'}]})
    else:
        updated = apply_travel_update(workflow, {
            'update_type': 'change',
            'target': {'workflow_id': workflow['id'], 'task_ids': [task_id],
                       'components': ['train']},
            'condition_updates': {'origin': '北京'}})
    assert updated['tasks'][0]['origin'] == '北京'
    assert updated['tasks'][0]['field_sources']['origin'] == 'user'
    assert workflow['tasks'][0]['field_sources']['origin'] == 'preference'
