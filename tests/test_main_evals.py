import json
from context.session_store import SessionStore
from evals.v0_memory.runner import evaluate_case, sourced_output_valid
from test_main_harness import weather_info


def test_evaluation_detects_failed_internal_tool(tmp_path):
    store = SessionStore(tmp_path, 'alice', 'tools')
    store.append_run({'type': 'agent_plan', 'turn_id': 'a', 'agents': [{'agent_name': 'information_query'}]})
    store.append({'type': 'stage_complete', 'turn_id': 'a', 'agent_name': 'information_query', 'status': 'success', 'content': {'data': weather_info()}})
    store.append({'type': 'tool_result', 'turn_id': 'a', 'scope': 'agent:information_query:1', 'status': 'error', 'content': {'status': 'error'}})
    store.append({'type': 'stage_complete', 'turn_id': 'a', 'agent_name': 'main_finalize', 'status': 'success', 'content': {'data': {'final_answer': '答复'}}})
    store.append_run({'type': 'query_run', 'turn_id': 'a'})
    result = evaluate_case({'id': 'tools', 'expected_agents': ['information_query']}, store)
    assert result['checks']['executed_route'] is True
    assert result['checks']['execution_success'] is False


def test_nested_forged_facts_are_rejected():
    info = weather_info()
    info['domain_results']['weather']['items'][0]['date'] = 'wrong'
    assert not sourced_output_valid([{'agent_name': 'information_query', 'content': {'data': info}}])
