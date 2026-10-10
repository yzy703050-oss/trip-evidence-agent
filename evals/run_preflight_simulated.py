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
from agents.contracts import INTENT_TYPES
from agents.execution_harness import ExecutionHarness
from agents.lazy_agent_registry import LazyAgentRegistry
from agents.main_agent import MainAgent
from config import LLM_CONFIG,RUN_LIMITS,get_model_generate_kwargs
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from evals.latency import summarize_latency
from evals.main_agent.live_runner import RecordingModel
from evals.simulated_travel import SimulatedTrainProvider,SimulatedHotelProvider

ROOT=Path(__file__).resolve().parents[1]


def assess_intents(case, responses):
    """Judge the model's actual initial JSON, before compatibility inference."""
    from utils.json_parser import robust_json_parse
    initial = [row for row in responses
               if row.get('stage') in {'main:plan', 'main:repair_decision'}]
    try:
        value = robust_json_parse(initial[-1]['text']) if initial else {}
        intents = value.get('intents')
        valid = isinstance(intents, list) and bool(intents) and all(
            isinstance(row, dict) and isinstance(row.get('type'), str)
            and row['type'] in INTENT_TYPES for row in intents)
    except (ValueError, TypeError, AttributeError):
        value, intents, valid = {}, [], False
    actual = {row['type'] for row in intents} if valid else set()
    expected = {'query_price': {'ask'}, 'explain': {'ask'}, 'memory_read': {'ask'},
        'hotel_replace': {'update'}, 'preference_replace': {'update'}, 'change_stay': {'update'},
        'supplement_origin': {'update'}, 'ambiguous': {'update'},
        'pause': {'control'}, 'resume': {'control'}, 'unsupported_purchase': {'control'},
        'preference_plan': {'update', 'plan'}}.get(case, {'plan'})
    checks = {'four_type_contract': valid, 'expected_purposes': actual == expected}
    if case in {'preference_plan', 'preference_replace'}:
        checks['preference_action_retained'] = any(
            row.get('agent_name') == 'preference' for row in value.get('agent_schedule', []))
    if case in {'explain', 'unsupported_purchase'}:
        checks['direct_without_dispatch'] = (
            value.get('response_mode') == 'direct' and not value.get('agent_schedule')
            and not value.get('travel_update'))
    if case == 'memory_read':
        names = {row.get('agent_name') for row in value.get('agent_schedule', [])}
        checks['memory_source'] = names == {'memory_query'} or (
            not names and value.get('response_mode') == 'direct' and bool(value.get('final_answer')))
    return dict(passed=all(checks.values()), checks=checks)


def assess(case,result,run,previous=None):
    w=result.get('workflow'); checks={'no_execution_error':result.get('status')!='error'}
    if case=='query_price':
        checks.update(no_workflow=w is None,train_result=bool(result.get('domain_results',{}).get('train',{}).get('items')))
    elif case in {'explain','pause','ambiguous','unsupported_purchase','memory_read'}:
        checks['no_external_requests']=run.external_request_count==0
        if case=='pause': checks['paused']=result.get('status')=='paused'
        if case=='ambiguous': checks['scope_checkpoint']=((w or {}).get('checkpoint') or {}).get('kind')=='feedback_scope'
        if case in {'unsupported_purchase', 'memory_read'}:
            checks['no_new_workflow'] = w is None
            checks['answer_returned'] = (
                bool(result.get('final_answer')) and result.get('status') == 'ok')
        if case == 'memory_read':
            brands = run.effective_preferences.get('hotel_brands')
            brands = [brands] if isinstance(brands, str) else brands
            checks['stored_preference_answer'] = bool(brands) and all(
                brand in result.get('final_answer', '') for brand in brands)
    else:
        checks['workflow_created']=isinstance(w,dict)
        if w:
            from agents.workflow_guard import check_workflow
            checked=check_workflow(w)
            checks['valid_candidate_references']=not any(i['code'] in {'invalid_candidate_reference','stale_draft','preserved_component_changed'} for i in checked['issues'])
            checks['one_or_three_tasks']=len(w['tasks'])==(3 if case.startswith('multi_route') else 1)
            checks['simulation_label']=case=='unknown_origin' or result.get('data_mode')=='simulation'
            checks['model_stay_proposals']=all(not t['requires_hotel'] or t['conditions'].get('nights') or
                (t['conditions'].get('check_in') and t['conditions'].get('check_out')) for t in w['tasks'])
            checks['no_invented_quote_requirement']=not w['confirmed_conditions'].get('hotel_quote_required') and not any(
                t['conditions'].get('hotel_quote_required') for t in w['tasks'])
            if case=='unknown_origin':
                checks['origin_checkpoint']=result['status']=='needs_input' and (w.get('checkpoint') or {}).get('kind')=='required_conditions'
                checks['no_external_queries']=run.external_request_count==0 and w['tasks'][0]['origin'] is None
            else:
                checks['usable_draft']=result['status']=='completed' and all(t.get('draft_plan') for t in w['tasks'])
                checks['no_personal_question']=w.get('checkpoint') is None
                if case.startswith('multi_route'):
                    checks['completed']=result['status']=='completed'
                    checks['return_no_hotel']=w['tasks'][-1]['purpose']=='return' and not w['tasks'][-1]['requires_hotel']
            if case=='arrival_overnight':
                plan=result.get('validated_plan',{}).get('reconstructed_tasks',[{}])[0]
                train=plan.get('train') or {}
                checks['previous_day_departure']=bool(train.get('arrival_at') and train['departure_at'][:10]<train['arrival_at'][:10])
            if case in {'hotel_replace', 'preference_replace'} and previous:
                checks['train_preserved']=w['tasks'][0]['draft_plan']['train_selection']==previous['workflow']['tasks'][0]['draft_plan']['train_selection']
                checks['no_train_request']=all(t['name']=='hotel_search' for t in run.tool_requests)
            if case=='supplement_origin' and previous:
                checks['same_workflow']=w['id']==previous['workflow_id']
                checks['default_not_drifted']=w['tasks'][0]['conditions']['departure_date']==previous['workflow']['tasks'][0]['conditions']['departure_date']
            if case in {'preference_plan', 'preference_replace'}:
                brands=run.effective_preferences.get('hotel_brands')
                # Existing preference values allow a scalar or a list. Also
                # verify the refreshed preference reached the actual query.
                checks['preference_updated']=brands in ('全季',['全季']) and any(
                    row.get('domain')=='hotel' and row.get('parameters',{}).get('keywords')=='全季'
                    for row in w.get('results_by_query',{}).values())
            if case == 'resume' and previous:
                checks['same_workflow'] = w['id'] == previous['workflow_id']
                checks['reuse_without_query'] = run.external_request_count == 0
            if case == 'change_stay':
                before = (previous['workflow']['tasks'][0]['conditions'].get('nights')
                          if previous else None)
                checks['stay_changed'] = (
                    before is not None and w['tasks'][0]['conditions'].get('nights') == before + 1
                    and w.get('last_update', {}).get('update_type') == 'change')
    return dict(passed=all(checks.values()),checks=checks)


def case_query(case, query, previous):
    if case == 'change_stay' and previous:
        nights = previous['workflow']['tasks'][0]['conditions'].get('nights')
        if nights is not None:
            return f'把刚才上海旅行的停留时间改为{nights + 1}晚，其他要求保留。'
    return query


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
           ('multi_route_unspecified_stay','multi-unspecified','我从上海去北京再去杭州最后回上海，帮我安排火车和酒店，没定日期和每站住几晚，请结合路线提出建议，返程上海不用酒店。'),
           ('hotel_replace','single','刚才上海那段换一家全季酒店，原来的火车和日期都别动。'),
           ('explain','single','解释为什么选这家上海酒店，不要修改或重新查询。'),
           ('ambiguous','single','刚才的安排我不满意。'),
           ('pause','single','这次上海旅行先暂停规划，不要查询。'),
           ('supplement_origin','unknown','重庆'),
           ('resume','single','继续刚才暂停的上海旅行规划，已查有效结果请复用，不要更改条件。'),
           ('change_stay','single','把刚才上海旅行的停留时间改为3晚，其他要求保留。'),
           ('preference_replace','single','以后酒店优先全季。这次上海旅行的酒店也换成全季，原火车和日期不变。'),
           ('memory_read','single','查询我已经保存的酒店品牌偏好，只读历史，不搜索网页。'),
           ('unsupported_purchase','single','帮我购买刚才方案里选中的火车票。')]
    if only: cases=[c for c in cases if c[0] in only]
    records=[]; previous={}
    for case,user,query in cases:
        print('START '+case,flush=True)
        memory=MemoryManager('simulated-'+user,case,storage_path=str(resume_memory or output/'memory'))
        known=memory.get_known_workflows()
        if user not in previous and known: previous[user]={'workflow_id':known[0]['id'],'workflow':known[0]}
        query = case_query(case, query, previous.get(user))
        generate=get_model_generate_kwargs(thinking=thinking)
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
        assessment = assess(case,result,run,previous.get(user))
        intent_assessment = assess_intents(case, responses)
        assessment['checks'].update(intent_assessment['checks'])
        assessment['passed'] = all(assessment['checks'].values())
        record=dict(case=case,query=query,status=result.get('status'),stop_reason=result.get('stop_reason'),
            assessment=assessment,
            latency=summarize_latency(calls,run.tool_requests,total_ms=elapsed,external_requests=run.external_request_count),
            source_mode='real_llm_simulated_travel',thinking_mode=thinking or LLM_CONFIG['thinking_mode'],trace_path=str(memory.session_store.session_dir))
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
    parser.add_argument('--thinking',choices=['disabled','low','high'],help='Comparison override; omission uses product default disabled')
    args=parser.parse_args()
    records=asyncio.run(evaluate(args.output,args.cases,resume_memory=args.resume_memory,thinking=args.thinking))
    raise SystemExit(0 if records and all(r['assessment']['passed'] for r in records) else 1)


if __name__=='__main__': main()
