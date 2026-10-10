import json
from types import SimpleNamespace
import pytest
from agents.contracts import validate_plan
from agents.main_agent import MainAgent
from agents.travel_updates import apply_travel_update
from agents.workflow_guard import check_workflow
from workflow_support import populated_workflow


def test_model_cannot_derive_a_hotel_quote_requirement_from_requiring_a_hotel():
    from agents.workflow_contracts import create_workflow
    value={'confirmed_conditions':{},'tasks':[{'origin':'上海','destination':'北京','requires_hotel':True,
        'conditions':{'hotel_quote_required':True},'field_sources':{'hotel_quote_required':'derived'}}]}
    with pytest.raises(ValueError,match='hotel quote requirement'):
        create_workflow(value,{'original_query':'只安排火车和酒店推荐'})
    value['tasks'][0]['field_sources']['hotel_quote_required']='user'
    assert create_workflow(value,{})['tasks'][0]['conditions']['hotel_quote_required'] is True


@pytest.mark.asyncio
async def test_prompt_has_all_feedback_intents_and_answer_first_rule():
    calls=[]
    async def model(messages,**kwargs):
        calls.append(messages[0]['content'])
        return SimpleNamespace(text=json.dumps(dict(response_mode='direct',finalization_mode='synthesize',agent_schedule=[],final_answer='你好')))
    await MainAgent(model).initialize(dict(original_query='你好'))
    for word in ['replace_hotel','change_route','adopt_plan','clarify_feedback_scope','trip_status_query','travel_update','arrival_before','purpose_source','今天+7','known_workflows']:
        assert word in calls[0]
    assert '首次不为个人字段追问' in calls[0]


@pytest.mark.parametrize('field,value',[('update_type','purchase'),('target',{'workflow_id':'other'})])
def test_invalid_update_contract_is_rejected_before_execution(field,value):
    w=populated_workflow()
    update=dict(update_type='replace',target=dict(workflow_id=w['id'],task_ids=[w['tasks'][0]['id']],components=['hotel']))
    update[field]=value
    with pytest.raises(ValueError):
        validate_plan(dict(response_mode='workflow',resume_workflow_id=w['id'],agent_schedule=[],travel_update=update))


@pytest.mark.parametrize('mode',[None,'direct'])
def test_explicit_travel_update_is_always_executed_not_merely_described(mode):
    value=dict(travel_update=dict(update_type='pause',target={'workflow_id':'w1'}))
    if mode: value.update(response_mode=mode,final_answer='暂停了')
    decision=validate_plan(value)
    assert decision['response_mode']=='workflow' and decision['resume_workflow_id']=='w1'


@pytest.mark.asyncio
async def test_truncated_initial_json_gets_one_bounded_repair():
    calls=[]
    async def model(messages,**kwargs):
        calls.append(messages)
        return SimpleNamespace(text='{"response_mode":' if len(calls)==1 else '{"response_mode":"direct","finalization_mode":"synthesize","agent_schedule":[],"final_answer":"你好"}')
    result=await MainAgent(model).initialize({'original_query':'你好'})
    assert len(calls)==2 and result['final_answer']=='你好'


def test_adopt_existing_candidate_does_not_query_or_erase_other_components():
    w=populated_workflow(); t=w['tasks'][0]; hotel=t['draft_plan']['hotel_selection']
    newer=apply_travel_update(w,dict(update_type='adopt',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['hotel']),
                                   selections={t['id']:{'hotel':hotel}}))
    assert newer['tasks'][0]['draft_plan']['train_selection']==t['draft_plan']['train_selection']
    assert newer['tasks'][0]['draft_plan']['hotel_selection']==hotel
    assert newer['tasks'][0]['user_adopted'] is True
    assert check_workflow(newer)['valid']


def test_arrival_supplement_releases_only_proposed_departure_date():
    w=populated_workflow(); t=w['tasks'][0]; t['field_sources']['departure_date']='default'
    value=apply_travel_update(w,dict(update_type='supplement',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['schedule']),
                                   condition_updates={'arrival_date':'2026-10-18'}))
    assert 'departure_date' not in value['tasks'][0]['conditions']
    t['field_sources']['departure_date']='user'
    fixed=apply_travel_update(w,dict(update_type='change',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['schedule']),condition_updates={'arrival_date':'2026-10-18'}))
    assert fixed['tasks'][0]['conditions']['departure_date']==t['conditions']['departure_date']


def test_constraint_supplement_does_not_delete_existing_hard_filters():
    w=populated_workflow(); t=w['tasks'][0]; t['conditions']['constraints']={'seat_class':'二等座'}
    newer=apply_travel_update(w,dict(update_type='change',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['train']),condition_updates={'constraints':{'departure_time_after':'12:00'}}))
    assert newer['tasks'][0]['conditions']['constraints']=={'seat_class':'二等座','departure_time_after':'12:00'}
