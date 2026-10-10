from copy import deepcopy
import json
from types import SimpleNamespace
import pytest

from agents.workflow_contracts import create_workflow
from agents.workflow_guard import check_task
from agents.workflow_queries import query_views, resolve_selection
from agents.travel_updates import apply_travel_update
from evals.simulated_travel import SimulatedTrainProvider, SimulatedHotelProvider
from travel_data.tools import ToolExecutor
from workflow_runtime import Runtime, WorkflowMain
from workflow_support import populated_workflow


class PreflightMain(WorkflowMain):
    async def step(self, context):
        t = context['current_task']
        if t['status'] == 'pending':
            return dict(action='dispatch', task_id=t['id'], mode='complete_conditions', goal='补全可执行条件',
                        query_requests=context['preflight']['query_requests'])
        value = await super().step(context)
        if value['action'] == 'draft_task':
            preserved = t.get('draft_plan') or {}
            for domain in {'train', 'hotel'} - set(t.get('update_scope', ['train', 'hotel'])):
                value['draft_plan'][domain+'_selection'] = preserved.get(domain+'_selection')
            rows = context['relevant_results']
            trains = [r['items'][0] for r in rows if r['domain'] == 'train' and r['items']]
            if t['requires_hotel'] and trains and trains[0].get('arrival_at'):
                from datetime import date, timedelta
                day = trains[0]['arrival_at'][:10]
                value['draft_plan']['schedule'] = dict(check_in=day, check_out=(date.fromisoformat(day)+timedelta(days=t['conditions']['nights'])).isoformat())
        if value['action'] == 'ask_user':
            return dict(action='finish', status='partial', final_answer='保留结果')
        return value


class PreflightInfoModel:
    async def __call__(self, messages, **kwargs):
        if len(messages) == 2:
            c = json.loads(messages[1]['content'])
            return SimpleNamespace(content=[dict(type='tool_use', id=f'q{i}',
                name='train_search_by_arrival' if 'arrival_date' in r['parameters'] else r['domain']+'_search',
                input=r['parameters']) for i, r in enumerate(c['task']['query_requests'])])
        return SimpleNamespace(text='{"summary":"模拟资料已整理"}')


def runtime(tmp_path, tasks, confirmed=None, *, session='s'):
    tasks = deepcopy(tasks)
    for task in tasks:
        if task['requires_hotel'] and not task.get('conditions', {}).get('nights'):
            task.setdefault('conditions', {})['nights'] = 1  # Explicit fake-model proposal, not a product default.
            task.setdefault('field_sources', {})['nights'] = 'proposal'
    r = Runtime(tmp_path, main=PreflightMain(dict(confirmed_conditions=confirmed or {}, tasks=tasks)),session=session)
    r.train = SimulatedTrainProvider(); r.hotel = SimulatedHotelProvider()
    r.info.model = PreflightInfoModel()
    r.info.tool_executor = ToolExecutor({'train_search': r.train, 'hotel_search': r.hotel})
    return r


@pytest.mark.asyncio
async def test_missing_origin_pauses_before_all_legs(tmp_path):
    r = runtime(tmp_path, [dict(origin=None, destination='上海', requires_hotel=True, conditions={}),
                          dict(origin='上海', destination='杭州', requires_hotel=False, conditions={'departure_date':'2026-10-20'})])
    result = await r.turn('我要去上海然后杭州，安排一下')
    assert result['status'] == 'needs_input'
    assert result['workflow']['checkpoint']['kind'] == 'required_conditions'
    assert not r.hotel.calls and not r.train.calls
    assert [t['status'] for t in result['workflow']['tasks']] == ['needs_input', 'pending']
    assert result['workflow']['tasks'][0]['conditions']['departure_date'] == '2026-10-17'
    assert 'origin' in result['missing_fields']


@pytest.mark.asyncio
async def test_arrival_preflight_returns_bound_evidence_and_completes(tmp_path):
    r = runtime(tmp_path, [dict(origin='重庆', destination='上海', requires_hotel=True,
                         conditions={'arrival_date':'2026-10-18'}, field_sources={'arrival_date':'user'})])
    result = await r.turn('18号到上海')
    assert result['status'] == 'completed'
    t = result['workflow']['tasks'][0]
    assert t['conditions']['departure_date'] == '2026-10-18'
    assert t['field_sources']['departure_date'] == 'derived'
    info = result['results'][0]['data']
    assert info['readiness'] == 'ready' and info['date_options'][0]['arrival_at'][:10] == '2026-10-18'
    assert info['execution']['external_requests'] == 2
    assert info['query_results'][0]['task_id'] == t['id']
    assert '模拟' in result['final_answer']


@pytest.mark.asyncio
@pytest.mark.parametrize('first_action',['ask_user','finish'])
async def test_early_personal_question_or_partial_gets_feedback_to_query_available_hotel(tmp_path,first_action):
    class EarlyStopMain(PreflightMain):
        asked=False
        async def step(self,context):
            if not self.asked:
                self.asked=True
                if first_action=='finish': return dict(action='finish',status='partial',final_answer='缺预算')
                return dict(action='ask_user',question='预算多少？',reason='缺预算',
                            affected_task_ids=[context['current_task']['id']],suggested_changes=[])
            if context['current_task']['status']=='pending':
                assert context['action_feedback']['code']=='available_queries_required'
            return await super().step(context)
    r=runtime(tmp_path,[dict(origin='重庆',destination='上海',requires_hotel=True,conditions={})])
    r.main=EarlyStopMain(r.main.p)
    r.harness.main_agent=r.main
    result=await r.turn('我要去上海玩，安排一下')
    assert result['status']=='completed' and result['workflow']['checkpoint'] is None
    assert len(r.hotel.calls)==1 and len(r.train.calls)==1
    assert result['workflow']['tasks'][0]['draft_plan']['hotel_selection']


def test_rejected_candidates_are_removed_from_view_and_cannot_be_selected():
    w = populated_workflow(); t=w['tasks'][0]; row=w['results_by_query'][t['query_ids'][0]]
    bad = row['items'][0]['id']; t['rejected_candidates']={'train':[bad]}
    assert bad not in [i['id'] for r in query_views(w,t['id']) for i in r['items']]
    with pytest.raises(ValueError):
        resolve_selection(w,t['id'],t['revision'],dict(query_id=row['id'],result_revision=1,candidate_id=bad))


def test_hotel_only_change_cannot_replace_preserved_train():
    w=populated_workflow(); t=w['tasks'][0]
    updated=apply_travel_update(w,dict(update_type='replace',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['hotel'])))
    t=updated['tasks'][0]; draft=deepcopy(t['draft_plan']); draft['train_selection']=None
    assert any(i['code']=='preserved_component_changed' for i in check_task(updated,t['id'],draft)['issues'])


@pytest.mark.asyncio
async def test_default_date_can_try_next_two_days_without_changing_user_date(tmp_path):
    r = runtime(tmp_path,[dict(origin='重庆',destination='上海',requires_hotel=False,conditions={})])
    from datetime import date
    r.train.empty_dates={date(2026,10,17)}
    result=await r.turn('去上海，安排火车')
    assert result['status']=='completed'
    assert [d.isoformat() for d in r.train.calls]==['2026-10-17','2026-10-18']
    assert result['workflow']['tasks'][0]['conditions']['departure_date']=='2026-10-18'
    assert result['workflow']['tasks'][0]['field_sources']['departure_date']=='proposal'


@pytest.mark.asyncio
async def test_required_origin_question_is_saved_without_calling_step(tmp_path):
    class Ask(PreflightMain):
        async def step(self,c):
            return dict(action='ask_user',question='你从哪里出发？',reason='缺出发地',affected_task_ids=[c['current_task']['id']],suggested_changes=[])
    r=runtime(tmp_path,[dict(origin=None,destination='上海',requires_hotel=True,conditions={})])
    r.main=Ask(r.main.p); r.harness.main_agent=r.main
    result=await r.turn('我要去上海')
    assert result['status']=='needs_input' and result['workflow']['checkpoint']['missing_fields']==['origin']
    assert not r.hotel.calls and not r.train.calls


@pytest.mark.asyncio
async def test_resuming_partial_with_saved_query_does_not_require_query_again(tmp_path):
    r=runtime(tmp_path,[dict(origin='重庆',destination='上海',requires_hotel=True,conditions={'constraints':{'hotel_max_nightly_cny':500}})])
    first=await r.turn('我要去上海')
    assert first['workflow']['tasks'][0]['draft_plan']['hotel_selection']
    r.main.resume={'id':first['workflow_id'],'update':{'task_updates':[]}}
    async def keep_partial(context):
        return dict(action='finish',status='partial',final_answer='已有酒店推荐，预算尚无法核实')
    r.main.step=keep_partial
    result=await r.turn('先保留现有部分方案')
    assert result['stop_reason']=='model_partial' and len(r.hotel.calls)==1
