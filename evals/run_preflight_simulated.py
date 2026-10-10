"""Real configured LLM, explicitly simulated travel providers, isolated memory."""
import argparse
import asyncio
import csv
from datetime import datetime,timedelta
import io
import json
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from agentscope.model import OpenAIChatModel
from rich.console import Console
from agents.contracts import RunState,RunLimits
from agents.execution_harness import ExecutionHarness
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.main_agent import MainAgent
from config import LLM_CONFIG,RUN_LIMITS
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from evals.latency import summarize_latency
from evals.main_agent.live_runner import RecordingModel
from evals.simulated_travel import SimulatedTrainProvider,SimulatedHotelProvider

ROOT=Path(__file__).resolve().parents[1]


def assess(case,result,run,previous=None):
    w=result.get('workflow'); checks={'no_execution_error':result.get('status')!='error'}
    if case=='query_price':
        checks.update(no_workflow=w is None,train_result=bool(result.get('domain_results',{}).get('train',{}).get('items')))
    elif case in {'explain','pause','ambiguous'}:
        checks['no_external_requests']=run.external_request_count==0
        if case=='pause': checks['paused']=result.get('status')=='paused'
        if case=='ambiguous': checks['scope_checkpoint']=(w or {}).get('checkpoint',{}).get('kind')=='feedback_scope'
    else:
        checks['workflow_created']=isinstance(w,dict)
        if w:
            from agents.workflow_guard import check_workflow
            checked=check_workflow(w)
            checks['valid_candidate_references']=not any(i['code'] in {'invalid_candidate_reference','stale_draft','preserved_component_changed'} for i in checked['issues'])
            checks['one_or_three_tasks']=len(w['tasks'])==(3 if case=='multi_route' else 1)
            checks['simulation_label']=result.get('data_mode')=='simulation'
            if case=='unknown_origin':
                checks['partial_without_question']=result['status']=='partial' and w['checkpoint'] is None
                checks['hotel_only']=run.external_request_count==1 and w['tasks'][0]['origin'] is None
            else:
                # No checkout date was supplied in single-city cases. The spec
                # permits a partial draft rather than inventing the stay duration.
                checks['usable_draft']=result['status'] in {'partial','completed'} and all(t.get('draft_plan') for t in w['tasks'])
                checks['no_personal_question']=w.get('checkpoint') is None
                if case=='multi_route':
                    checks['completed']=result['status']=='completed'
                    checks['return_no_hotel']=w['tasks'][-1]['purpose']=='return' and not w['tasks'][-1]['requires_hotel']
            if case=='arrival_overnight':
                plan=result.get('validated_plan',{}).get('reconstructed_tasks',[{}])[0]
                train=plan.get('train') or {}
                checks['previous_day_departure']=bool(train.get('arrival_at') and train['departure_at'][:10]<train['arrival_at'][:10])
            if case=='hotel_replace' and previous:
                checks['train_preserved']=w['tasks'][0]['draft_plan']['train_selection']==previous['workflow']['tasks'][0]['draft_plan']['train_selection']
                checks['no_train_request']=all(t['name']=='hotel_search' for t in run.tool_requests)
            if case=='supplement_origin' and previous:
                checks['same_workflow']=w['id']==previous['workflow_id']
                checks['default_not_drifted']=w['tasks'][0]['conditions']['departure_date']==previous['workflow']['tasks'][0]['conditions']['departure_date']
            if case=='preference_plan':
                brands=run.effective_preferences.get('hotel_brands')
                # Existing preference values allow a scalar or a list. Also
                # verify the refreshed preference reached the actual query.
                checks['preference_updated']=brands in ('全季',['全季']) and any(
                    row.get('domain')=='hotel' and row.get('parameters',{}).get('keywords')=='全季'
                    for row in w.get('results_by_query',{}).values())
    return dict(passed=all(checks.values()),checks=checks)


async def evaluate(output,only=None,*,resume_memory=None,thinking=None):
    output.mkdir(parents=True,exist_ok=True)
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    day=(now.date()+timedelta(days=8)).isoformat()
    cases=[('default_date','single','从重庆去上海，帮我安排火车和酒店，只做交通住宿，没有确定日期。'),
           ('unknown_origin','unknown','我要去上海玩，帮我安排火车和酒店，我还没说从哪里出发。'),
           ('arrival_overnight','overnight',f'我从重庆出发，准备{day}到上海，请安排火车和酒店，我没有指定出发日期。'),
           ('query_price','price',f'查{day}重庆到上海火车票价格，1人，只查火车，不规划旅程。'),
           ('preference_plan','preference','以后酒店优先全季。帮我安排重庆去上海的火车和酒店，还没确定日期。'),
           ('multi_route','multi','我从上海去北京再去杭州最后回上海。每站停留一晚，只安排火车和酒店，返程上海不用酒店，日期你提合理建议。'),
           ('hotel_replace','single','刚才上海那段换一家全季酒店，原来的火车和日期都别动。'),
           ('explain','single','解释为什么选这家上海酒店，不要修改或重新查询。'),
           ('ambiguous','single','刚才的安排我不满意。'),
           ('pause','single','这次上海旅行先暂停规划，不要查询。'),
           ('supplement_origin','unknown','补充刚才去上海的方案，我从重庆出发，出发日期沿用你的建议。')]
    if only: cases=[c for c in cases if c[0] in only]
    records=[]; previous={}
    for case,user,query in cases:
        print('START '+case,flush=True)
        memory=MemoryManager('simulated-'+user,case,storage_path=str(resume_memory or output/'memory'))
        known=memory.get_known_workflows()
        if user not in previous and known: previous[user]={'workflow_id':known[0]['id'],'workflow':known[0]}
        generate={'temperature':LLM_CONFIG.get('temperature',.7),'max_tokens':LLM_CONFIG.get('max_tokens',2000)}
        if thinking:
            generate['extra_body']={'thinking':{'type':'disabled' if thinking=='disabled' else 'enabled'}}
            if thinking!='disabled': generate['reasoning_effort']=thinking
        raw=OpenAIChatModel(model_name=LLM_CONFIG['model_name'],api_key=LLM_CONFIG['api_key'],
            client_kwargs={'base_url':LLM_CONFIG['base_url'],'timeout':60.0},
            generate_kwargs=generate)
        metered=MeteredModel(raw,memory.session_store.append_run)
        responses=[]; model=RecordingModel(metered,responses)
        train=SimulatedTrainProvider(duration_minutes=600,departure_hours=[20]) if user=='overnight' else SimulatedTrainProvider()
        hotel=SimulatedHotelProvider()
        registry=LazyAgentRegistry(model=model,cache={},memory_manager=memory,providers={'train_search':train,'hotel_search':hotel})
        registry.console=Console(file=io.StringIO())
        main=MainAgent(model); harness=ExecutionHarness(main,registry,memory)
        run=RunState(memory.start_turn(query),limits=RunLimits(**RUN_LIMITS),effective_preferences=memory.long_term.get_preference())
        started=perf_counter()
        try:
            result=await asyncio.wait_for(harness.run_turn(dict(original_query=query,current_time=now.isoformat()),run),timeout=650)
        except Exception as exc:
            result=dict(status='error',stop_reason=type(exc).__name__)
        elapsed=(perf_counter()-started)*1000
        memory.record_message('assistant',json.dumps(result,ensure_ascii=False),run.turn_id,final=True)
        calls=[c for c in memory.session_store.read_runs() if c.get('type')=='model_call' and c.get('turn_id')==run.turn_id]
        record=dict(case=case,query=query,status=result.get('status'),stop_reason=result.get('stop_reason'),
            assessment=assess(case,result,run,previous.get(user)),
            latency=summarize_latency(calls,run.tool_requests,total_ms=elapsed,external_requests=run.external_request_count),
            source_mode='real_llm_simulated_travel',thinking_mode=thinking or 'provider_default',trace_path=str(memory.session_store.session_dir))
        (output/(case+'.json')).write_text(json.dumps(dict(record=record,result=result,model_responses=responses),ensure_ascii=False,indent=2),encoding='utf-8')
        records.append(record)
        (output/'report.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:record[k] for k in ('case','status','stop_reason','assessment','latency')},ensure_ascii=False),flush=True)
        if result.get('workflow'): previous[user]=result
    with (output/'latency.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(['case','stage','calls','total_ms','average_ms','maximum_ms','input_tokens','output_tokens'])
        for r in records:
            for stage,s in r['latency']['stages'].items(): writer.writerow([r['case'],stage,*[s[k] for k in ('calls','total_ms','average_ms','maximum_ms','input_tokens','output_tokens')]])
    return records


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data'/'evals'/('preflight-simulated-'+datetime.now().strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--cases',nargs='*')
    parser.add_argument('--resume-memory',type=Path)
    parser.add_argument('--thinking',choices=['disabled','low','high'],help='Comparison only; omission uses unchanged product/provider defaults')
    args=parser.parse_args()
    records=asyncio.run(evaluate(args.output,args.cases,resume_memory=args.resume_memory,thinking=args.thinking))
    raise SystemExit(0 if records and all(r['assessment']['passed'] for r in records) else 1)


if __name__=='__main__': main()
