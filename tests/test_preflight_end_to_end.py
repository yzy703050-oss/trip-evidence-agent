import pytest
from workflow_runtime import Runtime,WorkflowMain
from test_preflight_workflow import runtime


@pytest.mark.asyncio
async def test_scope_checkpoint_can_be_paused_then_resumed_across_sessions(tmp_path):
    r = Runtime(tmp_path)
    first = await r.turn()
    workflow_id = first['workflow_id']
    async def clarify(context):
        return dict(response_mode='direct', intents=[{'type': 'update'}], agent_schedule=[],
            final_answer='改哪部分？', feedback_scope={'workflow_id': workflow_id, 'question': '改哪部分？'})
    r.main.initialize = clarify
    scoped = await r.turn('不满意')
    assert scoped['status'] == 'needs_input'
    async def pause(context):
        return dict(intents=[{'type': 'control'}], travel_update=dict(update_type='pause',
            target={'workflow_id': workflow_id}))
    r.main.initialize = pause
    paused = await r.turn('先暂停')
    assert paused['status'] == 'paused'
    assert paused['workflow']['checkpoint']['question'] == '改哪部分？'
    assert paused['workflow']['checkpoint']['expected_workflow_revision'] == paused['workflow_revision']
    restarted = Runtime(tmp_path, session='resume-scope')
    async def resume(context):
        return dict(response_mode='workflow', intents=[{'type': 'control'}],
                    resume_workflow_id=workflow_id, agent_schedule=[])
    restarted.main.initialize = resume
    result = await restarted.turn('继续规划')
    assert result['status'] == 'completed' and result['workflow_id'] == workflow_id
    assert not restarted.train.calls and not restarted.hotel.calls


@pytest.mark.asyncio
async def test_completed_trip_hotel_replacement_new_session_preserves_train(tmp_path):
    r=runtime(tmp_path,[dict(origin='重庆',destination='上海',requires_hotel=True,conditions={})])
    first=await r.turn('重庆去上海，安排交通住宿')
    assert first['status']=='completed'
    t=first['workflow']['tasks'][0]; old=t['draft_plan']['train_selection']; oldhotel=t['draft_plan']['hotel_selection']['candidate_id']
    restarted=runtime(tmp_path,r.main.p['tasks'],session='new-session')
    async def initialize(c):
        assert any(w['id']==first['workflow_id'] for w in c['known_workflows'])
        return dict(response_mode='workflow',resume_workflow_id=first['workflow_id'],agent_schedule=[],
            travel_update=dict(update_type='replace',target=dict(workflow_id=first['workflow_id'],task_ids=[t['id']],components=['hotel']),
                               condition_updates={'hotel_brands':['全季']},rejected_candidate_ids=[oldhotel]))
    restarted.main.initialize=initialize
    result=await restarted.turn('上海这段换全季，火车别动')
    assert result['status']=='completed'
    assert result['workflow']['tasks'][0]['draft_plan']['train_selection']==old
    assert not restarted.train.calls and len(restarted.hotel.calls)==1
    assert '全季' in result['validated_plan']['reconstructed_tasks'][0]['hotel']['hotel_name']
    assert result['workflow_id']==first['workflow_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,status',[('pause','paused'),('cancel','cancelled')])
async def test_stop_saves_state_without_external_queries(tmp_path,kind,status):
    r=Runtime(tmp_path); first=await r.turn()
    async def initialize(c):
        return dict(response_mode='workflow',resume_workflow_id=first['workflow_id'],agent_schedule=[],
            travel_update=dict(update_type=kind,target={'workflow_id':first['workflow_id']}))
    r.main.initialize=initialize; before=len(r.train.calls)
    result=await r.turn('停止规划')
    assert result['status']==status and len(r.train.calls)==before
    assert r.memory.workflow_store.load(first['workflow_id'])['status']==status


@pytest.mark.asyncio
async def test_ambiguous_feedback_saves_checkpoint_without_replanning(tmp_path):
    r=Runtime(tmp_path); first=await r.turn()
    async def initialize(c):
        return dict(response_mode='direct',finalization_mode='synthesize',agent_schedule=[],final_answer='你想修改哪一段的火车或酒店？',
            feedback_scope=dict(workflow_id=first['workflow_id'],question='你想修改哪一段的火车或酒店？'))
    r.main.initialize=initialize; before=len(r.train.calls)
    result=await r.turn('不满意')
    assert result['status']=='needs_input' and len(r.train.calls)==before
    saved=r.memory.workflow_store.load(first['workflow_id'])
    assert saved['checkpoint']['kind']=='feedback_scope'
    assert saved['tasks']==first['workflow']['tasks']


@pytest.mark.asyncio
async def test_new_trip_has_new_identity_and_preserves_previous_trip(tmp_path):
    r=Runtime(tmp_path); first=await r.turn()
    second=await r.turn('再安排一个新的旅行')
    assert second['workflow_id']!=first['workflow_id']
    assert r.memory.workflow_store.load(first['workflow_id'])==first['workflow']


@pytest.mark.asyncio
async def test_timeout_budget_includes_main_decisions_and_preserves_workflow(tmp_path,monkeypatch):
    import asyncio
    import config
    monkeypatch.setitem(config.WORKFLOW_LIMITS,'turn_timeout',.02)
    class Slow(WorkflowMain):
        async def step(self,c): await asyncio.Event().wait()
    r=Runtime(tmp_path,main=Slow())
    result=await r.turn()
    assert result['status']=='partial' and result['stop_reason']=='turn_timeout'
    assert r.memory.workflow_store.load(result['workflow_id'])['status']=='partial'
    assert not r.train.calls


@pytest.mark.asyncio
async def test_turn_timeout_also_includes_initial_intent_phase(tmp_path,monkeypatch):
    import asyncio
    import config
    monkeypatch.setitem(config.WORKFLOW_LIMITS,'turn_timeout',.02)
    class Slow(WorkflowMain):
        async def initialize(self,c): await asyncio.Event().wait()
    r=Runtime(tmp_path,main=Slow())
    result=await asyncio.wait_for(r.turn(),timeout=.5)
    assert result['status']=='partial' and result['stop_reason']=='turn_timeout'
    assert not r.train.calls


@pytest.mark.asyncio
async def test_price_only_request_queries_train_without_a_travel_workflow(tmp_path):
    import json
    from types import SimpleNamespace
    from agents.main_agent import MainAgent
    r=runtime(tmp_path,[dict(origin='重庆',destination='上海',requires_hotel=False,conditions={})])
    answers=[dict(response_mode='answer',finalization_mode='forward',agent_schedule=[dict(agent_name='information_query',requested_domains=['train'])]),
             [dict(type='tool_use',id='t',name='train_search',input=dict(origin='重庆',destination='上海',departure_date='2026-10-18',passengers=1))],
             dict(summary='模拟火车查询已整理')]
    async def model(messages,**kwargs):
        value=answers.pop(0)
        return SimpleNamespace(content=value) if isinstance(value,list) else SimpleNamespace(text=json.dumps(value,ensure_ascii=False))
    r.harness.main_agent=MainAgent(model); r.info.model=model
    result=await r.turn('只查票价，不安排旅行')
    assert result['status']=='ok' and 'workflow' not in result
    assert len(r.train.calls)==1 and not r.hotel.calls
    assert not r.memory.workflow_store.list_all()
    assert '模拟' in result['final_answer']


@pytest.mark.asyncio
async def test_explain_saved_trip_does_not_replan_or_query(tmp_path):
    r=Runtime(tmp_path); first=await r.turn()
    async def initialize(c):
        known=next(w for w in c['known_workflows'] if w['id']==first['workflow_id'])
        assert known['saved_plan']['reconstructed_tasks'][0]['hotel']
        return dict(response_mode='direct',agent_schedule=[],final_answer='根据已有酒店地点与出行条件作出的推荐，房价未知。')
    r.main.initialize=initialize; before=len(r.train.calls)
    result=await r.turn('为什么选这家酒店，不要改')
    assert result['status']=='ok' and len(r.train.calls)==before
    assert r.memory.workflow_store.load(first['workflow_id'])==first['workflow']


@pytest.mark.asyncio
async def test_explicit_return_hotel_is_queried_and_validated(tmp_path):
    r=runtime(tmp_path,[dict(origin='上海',destination='北京',purpose='visit',requires_hotel=False,conditions={'departure_date':'2026-10-17'}),
                       dict(origin='北京',destination='上海',purpose='return',requires_hotel=True,conditions={'departure_date':'2026-10-18'})])
    result=await r.turn('返回上海也推荐酒店')
    assert result['status']=='completed'
    assert len(r.hotel.calls)==1 and r.hotel.calls[0]['city']=='上海'
    assert result['validated_plan']['reconstructed_tasks'][1]['hotel']
