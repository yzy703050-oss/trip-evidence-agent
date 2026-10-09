import asyncio
import json
from types import SimpleNamespace
import pytest
from agentscope.message import Msg
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.orchestration_agent import OrchestrationAgent, normalize_schedule
from travel_data.contracts import AgentDataResult
from test_sourced_routing import event_agent


def run(fields, names, providers=None):
    async def model(messages):
        return SimpleNamespace(text=json.dumps(fields))
    registry = LazyAgentRegistry(None, {}, providers=providers or {})
    agents = {name: registry[name] for name in names}
    agents['event_collection'] = event_agent(model)
    request = {'agent_schedule': [{'agent_name': n, 'priority': 1e20} for n in names],
               'rewritten_query': '查询火车和攻略'}
    return json.loads(asyncio.run(OrchestrationAgent(agent_registry=agents).reply(Msg('u', json.dumps(request), 'user'))).content)


class Provider:
    def __init__(self, status='ok', fail=False): self.queries=[]; self.status=status; self.fail=fail
    async def search(self, query):
        self.queries.append(query)
        if self.fail: raise TimeoutError()
        return AgentDataResult(self.status, query.to_dict(), [], [], None, None, None)


@pytest.mark.parametrize('priority', [1e20, float('inf'), -float('inf'), float('nan')])
def test_extreme_schedule(priority):
    rows=normalize_schedule([{'agent_name': n, 'priority': priority} for n in ['itinerary_planning','train_search','hotel_search','event_collection']])
    p={r['agent_name']:r['priority'] for r in rows}
    assert p['event_collection'] < p['train_search'] == p['hotel_search'] < p['itinerary_planning']


@pytest.mark.parametrize('dates,expected', [({}, []), ({'start_date':'2026-10-20'}, ['2026-10-20']), ({'start_date':'2026-10-20','end_date':'2026-10-22'}, ['2026-10-20','2026-10-21','2026-10-22'])])
def test_collector_to_guide_dates(dates,expected):
    provider=Provider()
    run({'destination':'北京', **dates}, ['travel_guide'], {'travel_guide':provider})
    assert provider.queries[0].to_dict()['visit_dates']==expected


def test_separate_passenger_guest_queries():
    train, hotel=Provider(),Provider()
    run({'origin':'上海','destination':'北京','start_date':'2026-10-20','passengers':2,'guests':1,'check_in':'2026-10-20','check_out':'2026-10-22'}, ['train_search','hotel_search'], {'train_search':train,'hotel_search':hotel})
    assert train.queries[0].passengers==2 and hotel.queries[0].guests==1


@pytest.mark.parametrize('status', ['error','partial','needs_input','unavailable'])
def test_business_status_keeps_successful_sibling(status):
    result=run({'origin':'上海','destination':'北京','start_date':'2026-10-20'}, ['train_search','travel_guide'], {'train_search':Provider(status, fail=status=='error'),'travel_guide':Provider()})
    rows={r['agent_name']:r for r in result['results']}
    assert rows['train_search']['status']==status
    assert rows['travel_guide']['data']['status']=='ok'
    assert result['status']=='partial_failure'


@pytest.mark.parametrize('query', ['上海到北京火车', '北京攻略'])
def test_collection_does_not_require_hotel(query):
    async def model(messages): return SimpleNamespace(text=json.dumps({'destination':'北京','missing_info':['guests','check_in','check_out']}))
    result=json.loads(asyncio.run(event_agent(model).reply(Msg('u',query,'user'))).content)
    assert not set(['guests','check_in','check_out']).intersection(result['missing_info'])

@pytest.mark.parametrize('fields,count', [({'passengers':2,'guests':1},2), ({},1), ({'guests':2},1)])
def test_collector_provider_budget_consistency(fields,count):
    from travel_data.plan_guard import guard_itinerary
    from travel_data.contracts import Source
    from datetime import datetime, timezone
    class FareProvider(Provider):
        async def search(self, query):
            self.queries.append(query)
            source=Source('test-only',datetime(2026,10,8,tzinfo=timezone.utc),'https://example.org')
            offer={'id':'t1','train_number':'G1','origin_station':'Shanghai','destination_station':'Beijing','departure_time':'09:00','arrival_time':'14:00','seat_class':'second','price_cny':'500','availability':'available','remaining':None,'source':source.to_dict(),'url':None}
            return AgentDataResult('ok',query.to_dict(),[offer],[],source,source.fetched_at,None)
    provider=FareProvider()
    result=run({'origin':'Shanghai','destination':'Beijing','start_date':'2026-10-20',**fields},['train_search'],{'train_search':provider})
    assert provider.queries[0].passengers==count
    assert guard_itinerary({'selected_train_id':'t1'},result['results'])['budget']['known_subtotal_cny']==str(500*count)


@pytest.mark.parametrize('status',['error','partial','needs_input','unavailable'])
def test_edd_rejects_incomplete_business_state_even_old_success_wrapper(status):
    from evals.v0_memory.runner import evaluate_case
    result=run({'origin':'Shanghai','destination':'Beijing','start_date':'2026-10-20'},['train_search'],{'train_search':Provider(status,fail=status=='error')})
    stages=[{'type':'stage_complete','turn_id':'t1','agent_name':row['agent_name'],'status':'success','content':{'status':'success','data':row['data']}} for row in result['results']]
    store=SimpleNamespace(read_events=lambda:stages,read_runs=lambda:[{'type':'query_run','turn_id':'t1','run_seq':1}],user_id='test',session_id='test')
    evaluated=evaluate_case({'id':'test','expected_agents':['event_collection','train_search']},store)
    assert evaluated['checks']['execution_success'] is False
    assert 'train_search' in evaluated['metrics']['failed_agents']


@pytest.mark.parametrize('priority',[1e20,float('inf'),-float('inf'),float('nan')])
def test_actual_dependency_batches_at_extreme_priority(priority):
    calls = {}
    class Stub:
        def __init__(self,name): self.name=name
        async def reply(self,msg):
            calls[self.name]=[r['agent_name'] for r in json.loads(msg.content)['previous_results']]
            return Msg(self.name,json.dumps({'destination':'Beijing'}),'assistant')
    names=['itinerary_planning','train_search','hotel_search','event_collection']
    request={'agent_schedule':[{'agent_name':name,'priority':priority} for name in names]}
    asyncio.run(OrchestrationAgent(agent_registry={name:Stub(name) for name in names}).reply(Msg('u',json.dumps(request),'user')))
    assert calls['train_search']==calls['hotel_search']==['event_collection']
    assert set(calls['itinerary_planning'])=={'event_collection','train_search','hotel_search'}


def test_collector_prompt_requires_separate_counts():
    prompts = []
    async def model(messages):
        prompts.append(messages[0]['content'])
        return SimpleNamespace(text='{}')
    asyncio.run(event_agent(model).reply(Msg('u','two train passengers, one hotel guest','user')))
    assert 'passengers' in prompts[0] and 'guests' in prompts[0]
    assert 'Keep explicit train passenger and hotel guest counts separate' in prompts[0]


@pytest.mark.parametrize('domains,query,required', [(['train_search'],'hotel comparison mentioned in history',False), (['hotel_search'],'Beijing',True)])
def test_collector_hotel_requirements_follow_requested_domains(domains,query,required):
    async def model(messages): return SimpleNamespace(text='{}')
    request={'context':{'rewritten_query':query,'requested_domains':domains}}
    data=json.loads(asyncio.run(event_agent(model).reply(Msg('u',json.dumps(request),'user'))).content)
    assert set(['guests','check_in','check_out']).issubset(data['missing_info']) is required


@pytest.mark.parametrize('status',['needs_input','unavailable'])
def test_uniform_incomplete_domain_status_is_preserved(status):
    result=run({'destination':'Beijing'},['travel_guide'],{'travel_guide':Provider(status)})
    assert result['status']==status
