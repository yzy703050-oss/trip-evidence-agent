"""One bounded live evaluation run; not a product entrypoint."""
import asyncio
import argparse
from collections import Counter
from datetime import datetime
import io
import json
from pathlib import Path
import sys
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agentscope.model import OpenAIChatModel
from rich.console import Console
from agents.main_agent import MainAgent
from agents.execution_harness import ExecutionHarness
from agents.lazy_agent_registry import LazyAgentRegistry
from cli import TripEvidenceCLI
from config import LLM_CONFIG, get_settings, get_model_generate_kwargs
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.juhe_train import JuheTrainProvider
from evals.v0_memory.runner import evaluate_case
from agents.model_io import collect_model_turn
from context.telemetry import _stage

OUT = ROOT / 'data' / 'evals' / '2026-10-09-main-live'
RUN_ID = 'live-' + datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
USER = RUN_ID
WEATHER_CONTROL = False


class RecordingModel:
    def __init__(self, model, records):
        self.model, self.records = model, records

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def __call__(self, *args, **kwargs):
        stage = _stage.get()
        response = await self.model(*args, **kwargs)
        async def record(snapshot):
            turn = await collect_model_turn({'content': snapshot.content})
            self.records.append({'stage': stage, 'text': turn.text, 'tool_calls': turn.tool_calls})
        if hasattr(response, '__aiter__'):
            async def stream():
                last = None
                async for snapshot in response:
                    last = snapshot
                    yield snapshot
                await record(last)
            return stream()
        await record(response)
        return response

CASES = [
    ('weather', '北京现在天气怎么样？只查天气，不用规划行程。', ['information_query']),
    ('train', '查2026年10月10日北京南到苏州北的火车票，1人，二等座，预算每人700元以内。只查票，不规划行程。', ['information_query']),
    ('hotel_missing', '找几家北京的酒店，我还没确定入住离店日期和入住人数，请先告诉我还缺哪些条件，不要替我假定。', ['information_query']),
    ('hotel_unavailable', '查北京2026年10月10日入住、10月12日离店的酒店，1位成人，每晚不超过500元。只查酒店。', ['information_query']),
    ('preference_weather', '以后我的酒店品牌偏好改成汉庭。顺便查北京现在的天气，不用安排行程。', ['preference', 'information_query']),
    ('memory', '查询我已经保存的酒店品牌偏好是什么，只读我的历史记录，不要搜索网页。', ['memory_query']),
    ('itinerary', '帮我规划2026年10月10日从上海出发到北京，10月12日返程的三日行程。火车1人，酒店1人，10月10日入住、12日离店，火车预算单程不超过700元，酒店每晚500元内。不要查询企业制度。查不到的报价和景点信息要明确待核实，不要编造。', ['information_query']),
]


def assess(record):
    """Judge the requested behavior, including intentional unavailable/needs_input outcomes."""
    from decimal import Decimal
    expected = dict((case_id, expected) for case_id, _, expected in CASES)[record['case']]
    result, domains = record['result'], record['result'].get('domain_results', {})
    checks = {'completed': record['completed'] and record['exception_type'] is None,
        'single_final_message': record['final_messages'] == 1,
        'expected_route': set(record['planned_agents']) == set(expected),
        'no_model_error': not record['model_errors']}
    case = record['case']
    if case in {'weather', 'preference_weather'}:
        weather = domains.get('weather', {})
        checks['weather_sourced'] = weather.get('status') == 'ok' and bool(weather.get('items')) and bool(weather.get('source'))
        checks['current_weather_not_daily_forecast'] = weather.get('query', {}).get('date') is None and bool(weather.get('items')) and all(item.get('temp_c') is not None for item in weather.get('items', []))
        checks['no_unrelated_domain'] = set(domains) <= {'weather', 'web'} and ('web' not in domains or '天气' in domains['web'].get('query', {}).get('query', ''))
        checks['no_trip_saved'] = record['saved_trips'] == 0
        if case == 'weather':
            checks['forward_without_main_finalize'] = record['finalization_method'] == 'forward' and not record['model_stages'].get('main:finalize')
        else:
            checks['preference_saved'] = record['preferences'].get('hotel_brands') in ('汉庭', ['汉庭'])
            checks['preference_confirmed'] = '汉庭' in record['final_answer'] and '偏好' in record['final_answer']
    elif case == 'train':
        data = domains.get('train', {})
        items = data.get('items', [])
        checks['train_candidates'] = data.get('status') == 'ok' and 1 <= len(items) <= 5
        checks['hard_constraints'] = all(item.get('seat_class') == '二等座' and item.get('price_cny') is not None and Decimal(item['price_cny']) <= Decimal('700') for item in items)
        checks['only_train'] = set(domains) == {'train'}
    elif case == 'hotel_missing':
        missing = set(record['missing_fields'] or [])
        checks['asks_missing_conditions'] = record['status'] == 'needs_input' and all(
            bool(missing & alternatives) for alternatives in ({'check_in', '入住日期'}, {'check_out', '离店日期'}, {'guests', '入住人数'}))
        checks['no_external_query'] = not any(tool.get('execution', {}).get('query') for tool in record['tool_results'])
    elif case == 'hotel_unavailable':
        data = domains.get('hotel', {})
        checks['honest_unavailable'] = data.get('status') == 'unavailable' and not data.get('items') and record['status'] in {'unavailable', 'partial'}
    elif case == 'memory':
        checks['saved_brand_retrieved'] = record['status'] == 'ok' and '汉庭' in record['final_answer']
        checks['no_external_tools'] = not record['tool_results']
    elif case == 'itinerary':
        plan = result.get('itinerary', {})
        days = plan.get('itinerary', {}).get('daily_plans', [])
        checks['three_day_framework'] = {day.get('date') for day in days} == {'2026-10-10', '2026-10-11', '2026-10-12'}
        checks['honest_incomplete'] = plan.get('planning_complete') is False and record['saved_trips'] == 0
        for domain in ('train', 'weather'):
            queried = [tool['execution']['query'] for tool in record['tool_results']
                if tool['name'] == ('train_search' if domain == 'train' else 'weather_query') and tool['status'] == 'ok']
            current = domains.get(domain, {}).get('query')
            checks[f'{domain}_all_queries_retained'] = all(query == current for query in queried)
    return {'checks': checks, 'verdict': 'pass' if all(checks.values()) else 'fail'}


async def run_case(case_id, query, expected):
    memory = MemoryManager(USER, case_id, storage_path=str(OUT / 'memory'))
    raw = OpenAIChatModel(model_name=LLM_CONFIG['model_name'], api_key=LLM_CONFIG['api_key'],
        client_kwargs={'base_url': LLM_CONFIG['base_url'], 'timeout': 60.0},
        generate_kwargs=get_model_generate_kwargs())
    model_responses = []
    model = MeteredModel(RecordingModel(raw, model_responses), memory.session_store.append_run,
        input_usd_per_million=LLM_CONFIG['input_usd_per_million'],
        output_usd_per_million=LLM_CONFIG['output_usd_per_million'])
    registry = LazyAgentRegistry(model, {}, memory_manager=memory,
        providers={'train_search': JuheTrainProvider(get_settings().juhe_train_api_key)})
    buffer = io.StringIO()
    console = Console(file=buffer, width=240, color_system=None)
    registry.console = console
    if WEATHER_CONTROL:
        agent = registry['information_query']
        loader = agent.skill_loader
        class ControlLoader:
            def get_skill_content(self, name):
                content = loader.get_skill_content(name)
                return '\n'.join(line for line in content.splitlines() if not line.startswith('天气参数按用户所问的时段选择：'))
        agent.skill_loader = ControlLoader()
    app = TripEvidenceCLI()
    app.user_id, app.session_id, app.memory_manager, app.model = USER, case_id, memory, model
    app.console = console
    app.harness = ExecutionHarness(main_agent=MainAgent(model), agent_registry=registry, memory_manager=memory)
    started = perf_counter()
    exception = None
    try:
        completed = await asyncio.wait_for(app.process_query(query), timeout=240)
    except Exception as exc:
        completed, exception = False, type(exc).__name__
    result = app.last_result or {}
    events, runs = memory.session_store.read_events(), memory.session_store.read_runs()
    calls = [r for r in runs if r.get('type') == 'model_call']
    plans = [r for r in runs if r.get('type') == 'agent_plan']
    stages = [e for e in events if e.get('type') == 'stage_complete']
    tools = [e for e in events if e.get('type') == 'tool_result']
    final_messages = [e for e in events if e.get('type') == 'message' and e.get('role') == 'assistant' and e.get('final')]
    score = evaluate_case({'id': case_id, 'expected_agents': expected}, memory.session_store) if runs else {}
    summary = {'case': case_id, 'query': query, 'completed': completed, 'exception_type': exception,
        'weather_guidance_control': WEATHER_CONTROL,
        'status': result.get('status'), 'finalization_method': result.get('finalization_method'),
        'planned_agents': [a['agent_name'] for a in plans[-1].get('agents', [])] if plans else [],
        'actual_agents': [s.get('agent_name') for s in stages],
        'model_calls': len(calls), 'model_stages': dict(Counter(r.get('stage') for r in calls)),
        'model_errors': [{'stage': r.get('stage'), 'error': r.get('error')} for r in calls if r.get('status') == 'error'],
        'input_tokens': sum(r.get('input_tokens') or 0 for r in calls),
        'output_tokens': sum(r.get('output_tokens') or 0 for r in calls),
        'latency_ms': round((perf_counter()-started)*1000, 2),
        'tool_results': [{'name': e.get('name'), 'status': e.get('status'), 'execution': e.get('content', {}).get('execution')} for e in tools],
        'domain_statuses': {d: {'status': data.get('status'), 'count': len(data.get('items', [])), 'query': data.get('query'), 'message': data.get('message')} for d, data in result.get('domain_results', {}).items()},
        'missing_fields': result.get('missing_fields'), 'final_answer': result.get('final_answer'),
        'final_messages': len(final_messages), 'preferences': memory.long_term.get_preference(),
        'saved_trips': len(memory.long_term.get_trip_history()), 'evaluation': score,
        'result': result, 'display': buffer.getvalue(), 'model_responses': model_responses}
    summary['behavior_evaluation'] = assess(summary)
    serialized = json.dumps(summary, ensure_ascii=False, indent=2)
    for secret in (LLM_CONFIG['api_key'], get_settings().juhe_train_api_key):
        if secret:
            serialized = serialized.replace(secret, '[redacted]')
    target = OUT / RUN_ID
    target.mkdir(parents=True, exist_ok=True)
    (target / f'{case_id}.json').write_text(serialized, encoding='utf-8')
    print(json.dumps({k: summary[k] for k in ('case', 'status', 'finalization_method', 'planned_agents', 'model_calls', 'model_stages', 'latency_ms', 'domain_statuses', 'missing_fields', 'final_messages', 'saved_trips')}, ensure_ascii=False), flush=True)
    return json.loads(serialized)


async def main(selected=None, repeat=1):
    global RUN_ID, USER
    OUT.mkdir(parents=True, exist_ok=True)
    base_id = RUN_ID
    for repetition in range(repeat):
        RUN_ID = base_id if repeat == 1 else f'{base_id}-r{repetition+1}'
        USER = RUN_ID
        records = []
        for case in CASES:
            if selected and case[0] not in selected:
                continue
            print('START ' + RUN_ID + ' ' + case[0], flush=True)
            records.append(await run_case(*case))
            (OUT / RUN_ID / 'summary.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REPORT ' + str(OUT / RUN_ID / 'summary.json'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', help='Comma-separated case IDs; default all seven.')
    parser.add_argument('--repeat', type=int, default=1, help='Fresh isolated users per repetition; each run uses real APIs.')
    parser.add_argument('--weather-control', action='store_true', help='Remove only the new current-weather guidance from the information role for a comparison run.')
    args = parser.parse_args()
    WEATHER_CONTROL = args.weather_control
    selected = set(args.cases.split(',')) if args.cases else None
    if args.repeat < 1 or (selected and selected - {case[0] for case in CASES}):
        parser.error('positive repeat and known case IDs required')
    asyncio.run(main(selected, args.repeat))
