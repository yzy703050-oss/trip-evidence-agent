from copy import deepcopy
import pytest

from agents.workflow_contracts import create_workflow
from workflow_support import proposal
from test_preflight_workflow import runtime


def test_runtime_requires_model_stay_proposal_without_filling_nights():
    value = proposal()
    for row in value['tasks']:
        row['conditions'] = {}
    original = deepcopy(value)
    with pytest.raises(ValueError, match='stay proposal'):
        create_workflow(value, {}, require_stay_proposals=True)
    assert value == original


def test_runtime_preserves_different_model_stays_and_return_scope():
    value = proposal()
    for row, nights in zip(value['tasks'], (3, 2, None)):
        row['conditions'] = {'nights': nights} if nights else {}
        row['field_sources'] = {'nights': 'proposal'} if nights else {}
    result = create_workflow(value, {}, require_stay_proposals=True)
    assert [t['conditions'].get('nights') for t in result['tasks']] == [3, 2, None]
    assert result['tasks'][0]['field_sources']['nights'] == 'proposal'


def test_program_default_is_not_an_acceptable_stay_proposal():
    value = proposal()
    value['tasks'][0]['conditions']['nights'] = 2
    value['tasks'][0]['field_sources']['nights'] = 'default'
    with pytest.raises(ValueError, match='stay proposal'):
        create_workflow(value, {}, require_stay_proposals=True)


def test_replacing_only_train_does_not_query_previously_missing_hotel():
    from agents.travel_updates import apply_travel_update
    from workflow_support import populated_workflow
    w = populated_workflow()
    t = w['tasks'][0]
    t['draft_plan']['hotel_selection'] = None
    result = apply_travel_update(w, dict(update_type='replace',
        target=dict(workflow_id=w['id'], task_ids=[t['id']], components=['train'])))
    assert result['tasks'][0]['update_scope'] == ['train']


def test_false_user_quote_label_does_not_create_a_requirement():
    value = proposal()
    value['tasks'][0]['conditions']['hotel_quote_required'] = True
    value['tasks'][0]['field_sources']['hotel_quote_required'] = 'user'
    with pytest.raises(ValueError, match='hotel quote requirement'):
        create_workflow(value, {'original_query': '从上海到北京，只安排火车和酒店推荐'})
    assert create_workflow(value, {'original_query': '从上海到北京，请核实酒店报价'})['tasks'][0]['conditions']['hotel_quote_required']


@pytest.mark.parametrize('query', ['查火车价格和推荐酒店', '推荐酒店，价格不重要', '酒店不用报价，只推荐地点'])
def test_train_price_or_declined_hotel_quote_is_not_a_hotel_requirement(query):
    value = proposal()
    value['tasks'][0]['conditions']['hotel_quote_required'] = True
    value['tasks'][0]['field_sources']['hotel_quote_required'] = 'user'
    with pytest.raises(ValueError, match='hotel quote requirement'):
        create_workflow(value, {'original_query': query})


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_succeeds', [True, False])
async def test_missing_stay_gets_one_model_repair_and_no_program_fallback(tmp_path, repair_succeeds):
    from workflow_runtime import Runtime, WorkflowMain
    value = proposal()
    value['tasks'][0]['conditions'] = {}
    main = WorkflowMain(value)
    repairs = []
    async def repair(context, previous, error):
        repairs.append(error)
        result = deepcopy(previous)
        if repair_succeeds:
            result['tasks'][0]['conditions'] = {'nights': 3}
            result['tasks'][0]['field_sources'] = {'nights': 'proposal'}
        return result
    main.repair_proposal = repair
    r = Runtime(tmp_path, main=main)
    result = await r.turn()
    assert len(repairs) == 1 and 'stay proposal' in repairs[0]
    if repair_succeeds:
        assert result['workflow']['tasks'][0]['conditions']['nights'] == 3
    else:
        assert result['status'] == 'error' and not r.train.calls and not r.hotel.calls


@pytest.mark.asyncio
async def test_missing_origin_saves_question_before_external_requests(tmp_path):
    r = runtime(tmp_path, [dict(origin=None, destination='上海', requires_hotel=True,
                               conditions={'nights': 3}, field_sources={'nights': 'proposal'})])
    result = await r.turn('我要去上海，安排火车和酒店')
    assert result['status'] == 'needs_input'
    w = result['workflow']
    assert w['checkpoint']['kind'] == 'required_conditions'
    assert w['checkpoint']['missing_fields'] == ['origin']
    assert not r.train.calls and not r.hotel.calls
    assert w['tasks'][0]['conditions']['nights'] == 3
    assert '出发' in result['final_answer']


@pytest.mark.asyncio
async def test_origin_answer_resumes_same_task_and_queries_missing_hotel(tmp_path):
    r = runtime(tmp_path, [dict(origin=None, destination='上海', requires_hotel=True,
                               conditions={'nights': 3}, field_sources={'nights': 'proposal'})])
    first = await r.turn('我要去上海，安排火车和酒店')
    w = first['workflow']; task = w['tasks'][0]
    resumed = runtime(tmp_path, [], session='new')
    async def resume(context):
        return dict(response_mode='workflow', finalization_mode='synthesize', agent_schedule=[],
                    resume_workflow_id=w['id'], travel_update=dict(update_type='supplement',
                    target=dict(workflow_id=w['id'], task_ids=[task['id']], components=['train']),
                    condition_updates={'origin': '重庆'}))
    resumed.main.initialize = resume
    result = await resumed.turn('重庆')
    assert result['status'] == 'completed'
    assert result['workflow_id'] == w['id']
    updated = result['workflow']['tasks'][0]
    assert updated['id'] == task['id'] and updated['conditions']['nights'] == 3
    assert updated['conditions']['departure_date'] == task['conditions']['departure_date']
    assert len(resumed.train.calls) == len(resumed.hotel.calls) == 1


@pytest.mark.asyncio
async def test_harness_rejects_false_quote_requirement_in_a_feedback_update(tmp_path):
    r = runtime(tmp_path, [dict(origin='重庆', destination='上海', requires_hotel=True, conditions={'nights': 2})])
    first = await r.turn('从重庆去上海，安排火车酒店推荐')
    w = first['workflow']; t = w['tasks'][0]
    async def update(context):
        return dict(response_mode='workflow', agent_schedule=[], resume_workflow_id=w['id'],
                    travel_update=dict(update_type='change',
                        target=dict(workflow_id=w['id'], task_ids=[t['id']], components=['hotel']),
                        condition_updates={'hotel_quote_required': True}))
    r.main.initialize = update
    result = await r.turn('换一家酒店，其他不变')
    assert result['status'] == 'error'
    assert r.memory.workflow_store.load(w['id'])['revision'] == w['revision']
    assert len(r.train.calls) == len(r.hotel.calls) == 1
