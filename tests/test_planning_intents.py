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
async def test_prompt_uses_four_purposes_instead_of_feedback_catalog():
    calls=[]
    async def model(messages,**kwargs):
        calls.append(messages[0]['content'])
        return SimpleNamespace(text=json.dumps(dict(response_mode='direct',finalization_mode='synthesize',agent_schedule=[],final_answer='你好')))
    await MainAgent(model).initialize(dict(original_query='你好'))
    from agents.main_agent import INTENTS
    assert set(INTENTS) == {'ask', 'plan', 'update', 'control'}
    for word in ['travel_update','arrival_before','purpose_source','今天+7','known_workflows']:
        assert word in calls[0]
    for old in ['replace_hotel', 'replace_train', 'clarify_feedback_scope', 'trip_status_query', '完整意图目录']:
        assert old not in calls[0]
    assert '所有模式都输出 intents' in calls[0]
    assert '首次不为个人字段追问' in calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_succeeds', [True, False])
async def test_invalid_intent_gets_one_repair(repair_succeeds):
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        kind = 'ask' if repair_succeeds and len(calls) == 2 else 'replace_hotel'
        return SimpleNamespace(text=json.dumps(dict(response_mode='direct', final_answer='你好',
                                                     intents=[{'type': kind}], agent_schedule=[])))
    if repair_succeeds:
        result = await MainAgent(model).initialize({'original_query': '你好'})
        assert result['intents'] == [{'type': 'ask'}]
    else:
        with pytest.raises(ValueError, match='intent'):
            await MainAgent(model).initialize({'original_query': '你好'})
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_prompt_prioritizes_scope_question_over_update_execution():
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages[0]['content'])
        return SimpleNamespace(text=json.dumps(dict(response_mode='direct', final_answer='改哪部分？',
            intents=[{'type': 'update'}], agent_schedule=[],
            feedback_scope={'workflow_id': 'w1', 'question': '改哪部分？'})))
    result = await MainAgent(model).initialize({'original_query': '不满意'})
    assert result['response_mode'] == 'direct' and result['intents'] == [{'type': 'update'}]
    assert '先决定是否需要澄清，再决定是否执行' in calls[0]
    assert '范围不明禁止输出 travel_update' in calls[0]
    assert '已有依据能直接答复时必须 direct+synthesize' in calls[0]
    assert '购买车票不等于采纳建议' in calls[0]
    assert '查询已保存偏好使用memory_query，不调度preference' in calls[0]
    assert '换一家或替换组件必须使用replace' in calls[0]


@pytest.mark.asyncio
async def test_scope_repair_explicitly_removes_conflicting_update():
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        value = dict(response_mode='direct', intents=[{'type': 'update'}], agent_schedule=[],
                     final_answer='改哪部分？', feedback_scope={'workflow_id': 'w1', 'question': '改哪部分？'})
        if len(calls) == 1:
            value['travel_update'] = dict(update_type='regenerate', target={'workflow_id': 'w1'})
        return SimpleNamespace(text=json.dumps(value))
    result = await MainAgent(model).initialize({'original_query': '不满意'})
    assert len(calls) == 2 and 'travel_update' not in result
    assert '删除 travel_update' in calls[1][-1]['content']


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


@pytest.mark.asyncio
async def test_empty_change_gets_one_initial_repair_instead_of_replanning():
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        value = dict(response_mode='workflow', resume_workflow_id='w', agent_schedule=[],
                     travel_update=dict(update_type='change', target={'workflow_id':'w'}, condition_updates={}))
        if len(calls) > 1:
            value = dict(response_mode='direct', finalization_mode='synthesize', agent_schedule=[],
                         final_answer='想改哪一段的火车或酒店？',
                         feedback_scope=dict(workflow_id='w', question='想改哪一段的火车或酒店？'))
        return SimpleNamespace(text=json.dumps(value, ensure_ascii=False))
    result = await MainAgent(model).initialize({'original_query': '刚才的安排我不满意'})
    assert len(calls) == 2 and result['feedback_scope']['workflow_id'] == 'w'
