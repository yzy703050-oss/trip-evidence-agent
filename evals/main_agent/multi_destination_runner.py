"""Auditable workflow evaluation, using product CLI and executable acceptance tests."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]


def offline_coverage():
    return {
        'S01': ['test_three_and_five_tasks_have_drafts_then_global_validation'],
        'S02': ['test_three_and_five_tasks_have_drafts_then_global_validation'],
        'S03': ['test_three_and_five_tasks_have_drafts_then_global_validation', 'test_model_cannot_finish_without_check_or_spin_forever'],
        'S04': ['test_queries_do_not_overwrite_the_route_and_five_is_only_a_view'],
        'S05': ['test_verified_cross_day_cannot_check_in_before_arrival', 'test_unknown_arrival_day_cannot_be_invented_in_draft'],
        'S06': ['test_later_departure_cannot_precede_previous_stay', 'test_unknown_arrival_preserves_draft_without_personal_question'],
        'S07': ['test_global_party_change_is_confirmed_but_default_was_not', 'test_party_update_reuses_place_facts_not_old_train_pricing'],
        'S08': ['test_resume_second_task_preserves_first_and_reuses_unchanged_places'],
        'S09': ['test_restart_preserves_candidates_and_checkpoint', 'test_resume_second_task_preserves_first_and_reuses_unchanged_places'],
        'S10': ['test_return_empty_success_is_distinct_from_unavailable'],
        'S11': ['test_return_unavailable_is_not_claimed_as_no_trains', 'test_unavailable_provider_cannot_be_refreshed_repeatedly'],
        'S12': ['test_hotel_place_cannot_verify_hard_budget', 'test_decimal_budget_includes_every_train_and_hotel'],
        'S13': ['test_revision_change_invalidates_global_check', 'test_cli_displays_the_same_validated_route_without_old_guard'],
        'S14': ['test_invalid_action_cannot_start_provider', 'test_selection_requires_matching_task_query_revision_and_id'],
        'S15': ['test_tool_budget_is_cumulative_across_tasks', 'test_model_cannot_finish_without_check_or_spin_forever'],
        'S16': ['test_workflow_info_only_executes_train_hotel_and_does_not_mutate_confirmed', 'test_step_has_full_task_overview_and_no_activities_guide'],
        'S17': ['test_preference_runs_once_before_first_workflow_query', 'test_real_runtime_measures_actual_main_and_info_calls'],
        'S18': ['test_defaults_are_not_confirmed_and_three_tasks_have_stable_dependencies'],
        'S19': ['test_conditions_merge_without_overwriting_input_or_forcing_brand'],
        'S20': ['test_first_departure_can_inherit_explicit_start_date', 'test_global_fixed_start_date_cannot_be_silently_changed'],
        'S21': ['test_missing_date_can_still_query_hotel_places'],
        'S22': ['test_party_update_reuses_place_facts_not_old_train_pricing'],
    }


def evaluate_result(result, *, expected_tasks=None):
    from agents.workflow_guard import check_workflow, state_signature
    workflow = result.get('workflow')
    checks = {'response_not_error': result.get('status') != 'error'}
    complete = result.get('status') == 'completed'
    if expected_tasks is not None:
        checks['workflow_created'] = isinstance(workflow, dict)
        checks['task_count'] = isinstance(workflow, dict) and len(workflow.get('tasks', [])) == expected_tasks
    if workflow:
        try:
            checked = check_workflow(workflow)
            checks['only_train_hotel'] = all(r['domain'] in {'train', 'hotel'} for r in workflow['results_by_query'].values())
            checks['no_activity_output'] = all('activities' not in (t.get('draft_plan') or {}) for t in workflow['tasks'])
            checks['place_quotes_not_invented'] = all(item.get('stay_total_cny') is None and item.get('availability', 'unknown') == 'unknown'
                for row in workflow['results_by_query'].values() for item in row['items'] if item.get('kind') == 'hotel_place')
            if complete:
                checks['current_global_validation'] = checked['valid'] and bool(workflow.get('validation', {}).get('valid')) and workflow['validation'].get('signature') == state_signature(workflow)
                checks['all_current_tasks_validated'] = all(t['status'] == 'validated' and t['draft_plan']['task_revision'] == t['revision'] for t in workflow['tasks'])
            if result.get('status') == 'needs_input':
                checks['checkpoint_saved'] = bool((workflow.get('checkpoint') or {}).get('question'))
        except (ValueError, KeyError, TypeError, AttributeError):
            checks['valid_workflow_contract'] = False
    elif complete:
        checks['completed_needs_workflow'] = False
    rows = [row for stage in result.get('results', []) for row in stage.get('data', {}).get('query_results', [])]
    rows += list(result.get('domain_results', {}).values())
    query_verified = any(r.get('items') and r.get('source') and r.get('status') in {'ok', 'partial'} for r in rows)
    return {'passed': all(checks.values()), 'checks': checks, 'planning_complete': complete and all(checks.values()),
            'external_query_verified': query_verified, 'status': result.get('status'), 'stop_reason': result.get('stop_reason')}


def run_offline(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    xml_path = output.with_suffix('.xml')
    files = sorted(str(p.relative_to(ROOT)) for p in (ROOT/'tests').glob('test_workflow_*.py'))
    files += ['tests/test_main_end_to_end.py']
    command = [sys.executable, '-m', 'pytest', *files, '-q', f'--junitxml={xml_path}']
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace')
    output.with_suffix('.log').write_text(process.stdout+process.stderr, encoding='utf-8')
    tests = ET.parse(xml_path).getroot().findall('.//testcase') if xml_path.exists() else []
    scenarios = {}
    for scenario, names in offline_coverage().items():
        nodes = [t for name in names for t in tests if t.attrib['name'].split('[', 1)[0] == name]
        scenarios[scenario] = {'passed': bool(nodes) and all(t.find('failure') is None and t.find('error') is None and t.find('skipped') is None for t in nodes),
                               'tests': [t.attrib['name'] for t in nodes]}
    report = {'mode': 'offline', 'test_exit_code': process.returncode, 'test_count': len(tests), 'scenarios': scenarios,
              'passed': process.returncode == 0 and all(s['passed'] for s in scenarios.values())}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


async def run_live(output, case_ids=None):
    from rich.console import Console
    from cli import TripEvidenceCLI
    from context.memory_manager import MemoryManager
    cases = json.loads(Path(__file__).with_name('multi_destination_cases.json').read_text(encoding='utf-8'))
    if case_ids: cases = [c for c in cases if c['id'] in case_ids]
    run_id = 'workflow-live-'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    directory = ROOT/'data'/'evals'/run_id
    directory.mkdir(parents=True, exist_ok=True)
    start = (datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=1)).isoformat()
    records = []

    async def make_app(user_id, session):
        app = TripEvidenceCLI(); buffer = io.StringIO()
        app.console = Console(file=buffer, width=240, color_system=None)
        app.choose_session_id = lambda: session
        with patch('cli.Prompt.ask', return_value=user_id), patch('cli.MemoryManager', partial(MemoryManager, storage_path=str(directory/'memory'))):
            await app.initialize_system()
        app.orchestrator.agent_registry.console = app.console
        from evals.main_agent.live_runner import RecordingModel
        responses = []
        recording = RecordingModel(app.model, responses)
        app.main_agent.model = recording
        app.orchestrator.agent_registry.model = recording
        app.memory_manager.llm_model = recording
        app.evaluation_responses = responses
        return app, buffer

    async def execute(case, app, buffer):
        query = case['query'].format(start=start)
        try:
            await app.process_query(query)
            result = deepcopy(app.last_result or {'status': 'error'})
        except Exception:
            result = {'status': 'error', 'stop_reason': 'cli_exception'}
        turn_id = getattr(getattr(app, 'current_run', None), 'turn_id', None)
        calls = [r for r in app.memory_manager.session_store.read_runs() if r.get('type') == 'model_call' and r.get('turn_id') == turn_id]
        tools = deepcopy(getattr(getattr(app, 'current_run', None), 'tool_requests', []))
        record = {'id': case['id'], 'query': query, 'evaluation': evaluate_result(result, expected_tasks=case.get('tasks')),
                  'model_calls': len(calls), 'tool_calls': len(tools), 'external_requests': sum(not t.get('cache_hit') and bool(t.get('query')) for t in tools),
                  'workflow_id': result.get('workflow_id'), 'task_versions': [(t['id'], t['revision'], t['status']) for t in result.get('workflow', {}).get('tasks', [])],
                  'result_path': str(directory/f"{case['id']}.json"), 'trace_path': str(app.memory_manager.session_store.session_dir)}
        (directory/f"{case['id']}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory/f"{case['id']}.txt").write_text(buffer.getvalue(), encoding='utf-8')
        (directory/f"{case['id']}-model-responses.json").write_text(json.dumps(app.evaluation_responses, ensure_ascii=False, indent=2), encoding='utf-8')
        records.append(record)
        return result

    first = None
    for case in cases:
        app, buffer = await make_app(run_id+'-'+case['id'], case['id'])
        result = await execute(case, app, buffer)
        if case['id'] == 'three_return' and result.get('workflow'): first = (app.user_id, result)
        print(json.dumps(records[-1], ensure_ascii=False), flush=True)
    if first:
        user_id, previous = first
        app, buffer = await make_app(user_id, 'three_return')
        next_date = (datetime.fromisoformat(start)+timedelta(days=2)).date().isoformat()
        case = {'id': 'resume_second', 'tasks': 3, 'query': f"继续工作流 {previous['workflow_id']}，仅修改第二段：北京到杭州的出发日期改为{next_date}，在杭州住1晚，然后回上海，保留第一段要求。"}
        result = await execute(case, app, buffer)
        records[-1]['evaluation']['checks']['same_workflow_resumed'] = result.get('workflow_id') == previous['workflow_id']
        records[-1]['evaluation']['passed'] = all(records[-1]['evaluation']['checks'].values())
        print(json.dumps(records[-1], ensure_ascii=False), flush=True)
    report = {'mode': 'live', 'run_id': run_id, 'date': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'records': records, 'passed': bool(records) and all(r['evaluation']['passed'] for r in records)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main():
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True); mode.add_argument('--offline', action='store_true'); mode.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT/'data'/'evals'/'multi-destination-report.json')
    parser.add_argument('--case', action='append', dest='case_ids')
    args = parser.parse_args()
    report = run_offline(args.output) if args.offline else asyncio.run(run_live(args.output, args.case_ids))
    print(json.dumps({'passed': report['passed'], 'output': str(args.output)}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
