from copy import deepcopy
import pytest
from agents.workflow_runner import WorkflowRunner, WorkflowLimits
from agents.workflow_guard import check_workflow
from workflow_runtime import Runtime, WorkflowMain
from workflow_support import proposal


@pytest.mark.asyncio
@pytest.mark.parametrize('cities', [None, ['重庆', '上海', '苏州', '杭州', '北京', '重庆']])
async def test_three_and_five_tasks_have_drafts_then_global_validation(tmp_path, cities):
    r = Runtime(tmp_path, main=WorkflowMain(proposal(cities)))
    result = await r.turn()
    assert result['status'] == 'completed'
    n = len(result['workflow']['tasks'])
    assert len(r.train.calls) == n and len(r.hotel.calls) == n-1
    assert r.main.actions == ['commit_batch']
    assert all(t['status'] == 'validated' for t in result['workflow']['tasks'])
    assert len(r.main.contexts) == 1
    assert all(t['status'] == 'draft' and t['draft_stage'] == 'query' for t in r.main.contexts[0]['workflow']['tasks'])
    for context in r.main.contexts:
        assert all(len(row.get('items', [])) <= 5 for row in context['task_results'])
    assert max(len(row['items']) for row in result['workflow']['results_by_query'].values()) == 8
    assert r.main.initial_calls == 1
    assert 'activities' not in str(result['workflow']['tasks'])
    assert r.memory.workflow_store.load(result['workflow_id']) == result['workflow']


@pytest.mark.asyncio
async def test_model_cannot_finish_without_check_or_spin_forever(tmp_path):
    r = Runtime(tmp_path, main=WorkflowMain(finish_without_check=True))
    result = await r.turn()
    assert result['status'] == 'partial'
    assert result['stop_reason'] == 'no_progress'
    assert len(r.train.calls) == 3
    assert all(t['status'] == 'draft' and t['draft_stage'] == 'query' for t in result['workflow']['tasks'])


@pytest.mark.asyncio
async def test_unknown_arrival_preserves_draft_without_personal_question(tmp_path):
    r = Runtime(tmp_path); r.train.unknown_time = True
    result = await r.turn()
    assert result['status'] == 'partial'
    saved = r.memory.workflow_store.load(result['workflow_id'])
    assert saved['checkpoint'] is None
    assert len(saved['results_by_query']) >= 2


@pytest.mark.asyncio
async def test_resume_second_task_preserves_first_and_reuses_unchanged_places(tmp_path):
    r = Runtime(tmp_path); first = await r.turn(); w = first['workflow']
    restarted = Runtime(tmp_path, session='s')
    task = w['tasks'][1]
    restarted.main.resume = {'id': w['id'], 'update': {'task_updates': [{'task_id': task['id'], 'conditions': {'departure_date': '2026-10-14', 'check_in': '2026-10-14'}}]}}
    result = await restarted.turn('修改第二段，14号再出发')
    assert result['workflow_id'] == first['workflow_id']
    assert result['workflow']['tasks'][0] == first['workflow']['tasks'][0]
    assert len(restarted.train.calls) == 1
    assert not restarted.hotel.calls
    assert result['workflow']['tasks'][1]['revision'] == 2
    assert len(result['workflow']['results_by_query']) >= 6


@pytest.mark.asyncio
async def test_missing_date_can_still_query_hotel_places(tmp_path):
    p = proposal(); p['confirmed_conditions'] = {}
    for task in p['tasks']:
        task['conditions'] = {'nights': 2} if task['requires_hotel'] else {}
        task['field_sources'] = {'nights': 'proposal'} if task['requires_hotel'] else {}
    r = Runtime(tmp_path, main=WorkflowMain(p)); result = await r.turn()
    assert r.hotel.calls and r.train.calls
    assert result['status'] == 'completed'
    assert result['workflow']['tasks'][0]['field_sources']['departure_date'] == 'default'
    assert result['workflow']['effective_conditions']['passengers'] == 1


@pytest.mark.asyncio
async def test_failed_save_does_not_replay_queries(tmp_path, monkeypatch):
    r = Runtime(tmp_path)
    original = r.memory.workflow_store.save; count = 0
    def save(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3: raise OSError('offline disk error')
        return original(*args, **kwargs)
    monkeypatch.setattr(r.memory.workflow_store, 'save', save)
    result = await r.turn()
    assert result['status'] == 'error' and result['stop_reason'] == 'workflow_save_failed'
    assert len(r.train.calls) == 3
    assert len({str(q) for q in r.train.calls}) == 3


@pytest.mark.asyncio
async def test_tool_and_model_telemetry_has_task_association(tmp_path):
    r = Runtime(tmp_path); result = await r.turn()
    # Fake main does not call a model; provider and decision telemetry remain truthful.
    records = r.memory.session_store.read_runs()
    decisions = [e for e in records if e['type'] == 'workflow_decision']
    assert len(decisions) == 1 and decisions[0]['workflow_id'] == result['workflow_id']
    requests = result['workflow']['execution_plan']
    assert requests and all(e['task_id'] and e['request_id'] and e['completed_at'] for e in requests)
    assert not [e for e in records if e['type'] == 'model_call']


@pytest.mark.asyncio
async def test_invalid_action_cannot_start_provider(tmp_path):
    class Main(WorkflowMain):
        async def step(self, context): return {'action': 'book_and_pay'}
    r = Runtime(tmp_path, main=Main()); result = await r.turn()
    assert result['status'] == 'error' and result['stop_reason'] == 'invalid_workflow_action'
    assert len(r.train.calls) == 3 and len(r.hotel.calls) == 2


@pytest.mark.asyncio
async def test_unavailable_provider_cannot_be_refreshed_repeatedly(tmp_path):
    class Main(WorkflowMain):
        async def step(self, context):
            task = context['current_task']
            return {'action': 'dispatch', 'task_id': task['id'], 'goal': '重查', 'query_requests': [
                {'domain': 'train', 'parameters': dict(origin=task['origin'], destination=task['destination'],
                departure_date=task['conditions']['departure_date'], refresh=True)}]}
    r = Runtime(tmp_path, main=Main()); r.train.status = 'unavailable'
    result = await r.turn()
    assert result['status'] == 'partial'
    assert len(r.train.calls) == 3
    assert len({str(q) for q in r.train.calls}) == 3
    assert result['stop_reason'] == 'no_progress'


@pytest.mark.asyncio
async def test_explicit_preference_updates_memory_before_plan_without_preference_agent(tmp_path):
    from agents.runtime import Msg
    import json
    class Main(WorkflowMain):
        async def initialize(self, context):
            value = await super().initialize(context)
            value['agent_schedule'] = [{'agent_name': 'preference', 'priority': 1}]
            return value
    r = Runtime(tmp_path, main=Main())
    class Preference:
        calls = 0
        async def reply(self, msg):
            self.calls += 1
            assert not r.train.calls
            return Msg('preference', json.dumps({'preferences': {'hotel_brands': ['汉庭']}}), 'assistant')
    pref = Preference(); r.harness.agent_registry['preference'] = pref
    result = await r.turn('以后尽量选择汉庭，安排火车酒店')
    assert result['status'] == 'completed' and pref.calls == 0
    assert r.memory.long_term.data['travel_memories'][0]['text'] == '以后尽量选择汉庭'
    assert r.main.contexts[0]['effective_preferences']['memories'][0]['source'] == 'explicit'


@pytest.mark.asyncio
async def test_tool_budget_is_cumulative_across_tasks(tmp_path, monkeypatch):
    import config
    monkeypatch.setitem(config.WORKFLOW_LIMITS, 'tools_per_task', 1)
    r = Runtime(tmp_path); result = await r.turn()
    assert len(r.train.calls) + len(r.hotel.calls) <= 3
    assert result['status'] != 'completed'
    assert result['workflow']['results_by_query']


@pytest.mark.asyncio
async def test_final_prose_cannot_introduce_fake_prices_or_booking(tmp_path):
    class Main(WorkflowMain):
        async def step(self, context):
            value = await super().step(context)
            if value['action'] == 'finish': value['final_answer'] = '酒店总价299999元，已预订成功。'
            return value
    r = Runtime(tmp_path, main=Main()); result = await r.turn()
    assert result['status'] == 'completed'
    assert '299999' not in result['final_answer'] and '已预订' not in result['final_answer']
    assert 'G0' in result['final_answer'] and '酒店' in result['final_answer']


@pytest.mark.asyncio
async def test_return_unavailable_is_not_claimed_as_no_trains(tmp_path):
    r = Runtime(tmp_path)
    original = r.train.search
    async def search(query):
        if query.destination == '上海': r.train.status = 'unavailable'
        return await original(query)
    r.train.search = search
    result = await r.turn()
    records = [q for q in result['workflow']['results_by_query'].values() if q['domain'] == 'train']
    assert records[-1]['status'] == 'unavailable' and records[0]['items']
    assert result['status'] != 'completed'
    assert '没有火车' not in result['final_answer']


@pytest.mark.asyncio
async def test_next_date_derives_from_previous_checkout_when_flexible(tmp_path):
    p = proposal(); p['confirmed_conditions']['flexible_dates'] = True
    p['tasks'][1]['conditions'].pop('departure_date')
    p['tasks'][1]['field_sources'].pop('departure_date')
    r = Runtime(tmp_path, main=WorkflowMain(p)); result = await r.turn()
    task = result['workflow']['tasks'][1]
    assert task['conditions']['departure_date'] == '2026-10-13'
    assert task['field_sources']['departure_date'] == 'derived'
    assert result['status'] == 'completed'


@pytest.mark.asyncio
async def test_party_update_reuses_place_facts_not_old_train_pricing(tmp_path):
    r = Runtime(tmp_path); first = await r.turn()
    restarted = Runtime(tmp_path)
    restarted.main.resume = {'id': first['workflow_id'], 'update': {'confirmed_conditions': {'passengers': 3}}}
    result = await restarted.turn('实际是三个人')
    assert not restarted.hotel.calls
    assert len(restarted.train.calls) == 3
    assert all(q['passengers'] == 3 for q in restarted.train.calls)
    assert result['validated_plan']['budget']['known_subtotal_cny'] == '900'


@pytest.mark.asyncio
async def test_return_empty_success_is_distinct_from_unavailable(tmp_path):
    r = Runtime(tmp_path); original = r.train.search
    async def search(query):
        result = await original(query)
        if query.destination == '上海': result.items.clear()
        return result
    r.train.search = search
    result = await r.turn()
    rows = [q for q in result['workflow']['results_by_query'].values() if q['domain'] == 'train']
    assert rows[-1]['status'] == 'ok' and rows[-1]['items'] == []
    assert result['status'] == 'partial'
    assert result['workflow']['checkpoint'] is None


@pytest.mark.asyncio
async def test_resume_decision_cannot_apply_to_a_newer_revision(tmp_path):
    r = Runtime(tmp_path); first = await r.turn()
    restarted = Runtime(tmp_path)
    original = restarted.main.initialize
    restarted.main.resume = {'id': first['workflow_id'], 'update': {'confirmed_conditions': {'passengers': 3}}}
    async def initialize(context):
        decision = await original(context)
        w = restarted.memory.workflow_store.load(first['workflow_id'])
        restarted.memory.workflow_store.save(w, expected_revision=w['revision'])
        return decision
    restarted.main.initialize = initialize
    result = await restarted.turn('实际3人')
    assert result['status'] == 'error' and result['stop_reason'] == 'workflow_revision_conflict'
    assert not restarted.train.calls
    assert restarted.memory.workflow_store.load(first['workflow_id'])['confirmed_conditions'].get('passengers') is None


@pytest.mark.asyncio
async def test_expired_price_evidence_is_not_an_eternal_booking_guarantee(tmp_path):
    from agents.contracts import RunState
    from workflow_support import populated_workflow
    class Main:
        context = None
        async def step(self, context):
            self.context = context
            return {'action': 'finish', 'status': 'partial', 'final_answer': '旧报价需要重确认'}
    r = Runtime(tmp_path); w = r.memory.workflow_store.save(populated_workflow(), expected_revision=None)
    main = Main(); runner = WorkflowRunner(main, r.info, r.memory)
    result = await runner.run({'original_query': '继续', 'current_time': '2026-10-11T10:00:00+08:00'}, RunState(r.memory.start_turn('继续')), w)
    assert 'history' not in main.context['relevant_results'][0]
    assert len(r.train.calls) == 3  # Expired quotes are queried once per affected task.
    assert result['validated_plan']['valid'] is False
    assert result['validated_plan']['budget']['verified'] is False
    assert result['status'] == 'partial'


@pytest.mark.asyncio
async def test_one_invalid_action_can_be_corrected_without_executing_it(tmp_path):
    class Main(WorkflowMain):
        attempts = 0
        async def step(self, context):
            self.attempts += 1
            if self.attempts == 1: return {'action': 'dispatch', 'task_id': context['current_task']['id'], 'legacy_field': []}
            assert context['action_feedback'] is not None or self.attempts > 2
            return await super().step(context)
    r = Runtime(tmp_path, main=Main()); result = await r.turn()
    assert result['status'] == 'completed'
    assert len(r.train.calls) == 3


@pytest.mark.asyncio
async def test_invalid_proposal_gets_one_schema_repair_before_any_tools(tmp_path):
    class Main(WorkflowMain):
        repairs = 0
        async def initialize(self, context):
            value = await super().initialize(context)
            value['workflow_proposal']['confirmed_conditions']['constraints'] = ['只查火车酒店']
            return value
        async def repair_proposal(self, context, value, error):
            self.repairs += 1
            assert not r.train.calls
            fixed = deepcopy(value); fixed['confirmed_conditions']['constraints'] = {}
            return fixed
    main = Main(); r = Runtime(tmp_path, main=main)
    result = await r.turn()
    assert result['status'] == 'completed' and main.initial_calls == 1 and main.repairs == 1


@pytest.mark.asyncio
async def test_explicit_refresh_only_queries_target_train_and_preserves_other_rows(tmp_path):
    first = await Runtime(tmp_path).turn()
    restarted = Runtime(tmp_path)
    target = first['workflow']['tasks'][1]
    async def initialize(context):
        return {'response_mode': 'workflow', 'agent_schedule': [], 'resume_workflow_id': first['workflow_id'],
            'refresh': True, 'refresh_target': {'task_ids': [target['id']], 'components': ['train']}}
    restarted.main.initialize = initialize
    result = await restarted.turn('只重查第二段最新余票')
    assert result['status'] == 'completed'
    assert len(restarted.train.calls) == 1 and not restarted.hotel.calls
    assert result['workflow']['tasks'][0] == first['workflow']['tasks'][0]
