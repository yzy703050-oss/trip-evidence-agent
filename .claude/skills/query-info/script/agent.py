"""Information acquisition agent with validated tools and bounded follow-up."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4
from time import perf_counter

from agentscope.agent import AgentBase
from agentscope.message import Msg
from agents.contracts import RunState
from agents.model_io import collect_model_turn, to_assistant_tool_message, to_tool_message
from context.telemetry import model_stage
from utils.json_parser import robust_json_parse
from utils.skill_loader import SkillLoader
from travel_data.tools import ToolExecutor, TOOL_DOMAINS
from travel_data.result_guard import guard_information_result, grounded_answer
from travel_data.candidates import candidate_view


class InformationQueryAgent(AgentBase):
    def __init__(self, name='InformationQueryAgent', model=None, tool_executor=None, memory_manager=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model
        self.tool_executor = tool_executor or ToolExecutor({})
        self.memory_manager = memory_manager
        self.skill_loader = SkillLoader()

    async def reply(self, msg=None):
        payload = msg[-1].content if isinstance(msg, list) else msg.content if msg else {}
        try:
            payload = json.loads(payload) if isinstance(payload, str) else payload
        except ValueError:
            payload = {'context': {'original_query': str(payload)}}
        context = payload.get('context', payload)
        result = await self.run(context, RunState(uuid4().hex,
            effective_preferences=context.get('effective_preferences', context.get('user_preferences', {}))))
        return Msg(self.name, json.dumps(result, ensure_ascii=False), 'assistant')

    async def run(self, context, run):
        started = perf_counter()
        workflow_mode = context.get('type') == 'task_request'
        task_request = context.get('task', {}) if workflow_mode else {}
        if workflow_mode:
            context = {**context['context'], 'task': task_request,
                       'requested_domains': task_request['requested_domains'], 'type': 'task_request'}
            if run.workflow is None or run.current_task_id != task_request['id']:
                raise ValueError('task request does not match workflow')
            from agents.workflow_contracts import task_by_id
            if task_by_id(run.workflow, run.current_task_id)['revision'] != task_request['revision']:
                raise ValueError('stale task request')
        initial_tool_count = len(run.tool_requests)
        initial_external_count = run.external_request_count
        run.info_executions += 1
        requested = context.get('requested_domains', [])
        feedback = context.get('feedback') or {}
        messages = [{'role': 'system', 'content': self.skill_loader.get_skill_content('query-info') or '根据原文整理条件，使用工具查询并总结。'},
            {'role': 'user', 'content': json.dumps({**context, 'effective_preferences': run.effective_preferences,
                'travel_conditions': run.travel_conditions, 'domain_results': run.domain_results}, ensure_ascii=False)}]
        summary, limited, tool_count, model_count = '', False, 0, 0
        seen_ids = set()
        final_payload = {}
        for _ in range(run.limits.info_model_calls):
            try:
                with model_stage('agent:information_query'):
                    schemas = self.tool_executor.schemas()
                    if workflow_mode:
                        schemas = [s for s in schemas if TOOL_DOMAINS[s['function']['name']] in requested]
                    model_count += 1
                    turn = await collect_model_turn(await self.model(messages, tools=schemas, tool_choice='auto'))
                if not turn.tool_calls:
                    final_payload = robust_json_parse(turn.text)
                    summary = final_payload.get('summary', '')
                    if not isinstance(summary, str):
                        raise ValueError('summary must be text')
                    # Extras (purpose, duration) cannot replace actual queried fields.
                    extras = final_payload.get('travel_conditions', {})
                    if isinstance(extras, dict) and not workflow_mode:
                        for key, value in extras.items():
                            if key not in run.travel_conditions:
                                run.travel_conditions[key] = value
                    break
                messages.append(to_assistant_tool_message(turn))
                async def execute(call, allowed):
                    if not allowed:
                        return {'status': 'error', 'message': '工具调用达到上限或 ID 重复。', 'items': []}
                    if (feedback or workflow_mode) and TOOL_DOMAINS.get(call['name']) not in requested:
                        return {'status': 'error', 'message': '补查只能查询受影响领域。', 'items': []}
                    arguments = deepcopy(call['arguments'])
                    if feedback:
                        changes = feedback.get('constraints', {})
                        local = {key: val for key, val in changes.items() if key not in {'candidate_offset', 'refresh'}}
                        arguments['constraints'] = {**arguments.get('constraints', {}), **local}
                        for key in ('candidate_offset', 'refresh'):
                            if key in changes:
                                arguments[key] = changes[key]
                    execution_id = f"{run.turn_id}:info{run.info_executions}:{call['id']}"
                    return await self.tool_executor.execute(call['name'], arguments, run, call_id=execution_id)
                allowed = []
                for call in turn.tool_calls:
                    in_scope = not workflow_mode or TOOL_DOMAINS.get(call['name']) in requested
                    valid = call['id'] not in seen_ids and tool_count < run.limits.info_tool_calls and in_scope
                    if workflow_mode and run.workflow_tool_budget is not None:
                        valid = valid and run.workflow_tool_budget > 0
                        if valid: run.workflow_tool_budget -= 1
                    allowed.append(valid)
                    if valid:
                        tool_count += 1
                        seen_ids.add(call['id'])
                    else:
                        limited = True
                results = await asyncio.gather(*(execute(call, ok) for call, ok in zip(turn.tool_calls, allowed)))
                for call, result in zip(turn.tool_calls, results):
                    messages.append(to_tool_message(call['id'], result))
                    self._record_tool(run, call, result)
            except Exception:
                limited = True
                break
        else:
            limited = True
        # A selected view may contain IDs from earlier windows in this same pool.
        selected_ids = final_payload.get('selected_ids', {})
        if isinstance(selected_ids, dict) and run.candidates and not workflow_mode:
            for domain, ids in selected_ids.items():
                if domain not in {'train', 'hotel'} or not isinstance(ids, list):
                    continue
                eligible = {}
                for key, raw in run.candidates.cache.items():
                    if key.startswith(domain + ':') and raw.query == run.domain_results.get(domain, {}).get('query'):
                        view = candidate_view(raw, limit=len(raw.items), offset=0,
                            constraints=run.travel_conditions.get('constraints', {}), preferences=run.effective_preferences)
                        eligible.update({item['id']: item for item in view['items']})
                if any(item_id not in eligible for item_id in ids):
                    limited = True
                    continue
                if domain in run.domain_results:
                    data = run.domain_results[domain]
                    data['items'] = [eligible[item_id] for item_id in dict.fromkeys(ids)][:run.limits.candidate_limit]
                    if data['items'] and data['status'] not in {'ok', 'partial'}:
                        # A failed refresh must not label previously sourced facts as a fresh success.
                        data['status'] = 'partial'
                        data['source'] = deepcopy(data['items'][0]['source'])
                        data['fetched_at'] = data['source'].get('fetched_at') if isinstance(data['source'], dict) else None
                        data['message'] = (data.get('message') or '刷新未完成。') + '保留此前取得的候选，需重新确认报价和库存。'
        if workflow_mode:
            from agents.workflow_queries import query_views
            from agents.planning_conditions import prepare_task
            rows = query_views(run.workflow, run.current_task_id, limit=run.limits.candidate_limit)
            calls = run.tool_requests[initial_tool_count:]
            task = task_by_id(run.workflow, run.current_task_id)
            options = []
            for row in rows:
                if row['domain'] != 'train' or row.get('needs_revalidation'): continue
                for item in row['items']:
                    if item.get('departure_at') and item.get('arrival_at'):
                        options.append(dict(query_id=row['id'], result_revision=row['result_revision'], candidate_id=item['id'],
                                            departure_at=item['departure_at'], arrival_at=item['arrival_at'], source=item['source']))
            if options and not task['conditions'].get('departure_date'):
                task['conditions']['departure_date'] = options[0]['departure_at'][:10]
                task['field_sources']['departure_date'] = 'derived'
            prepared = prepare_task(run.workflow, run.current_task_id, context.get('current_time'), previous_boundary=context.get('previous_boundary'))
            missing = sorted(set(prepared['missing_fields']) | {f for r in rows for f in r.get('missing_fields', [])})
            statuses = [r['status'] for r in rows]
            status = 'ok' if not missing and statuses and all(s == 'ok' for s in statuses) and not limited else 'partial'
            if statuses and len(set(statuses)) == 1 and statuses[0] in {'unavailable', 'error'}:
                status = statuses[0]
            return dict(type='task_result', task_id=task_request['id'], task_revision=task_request['revision'],
                        agent='information_query', status=status, summary=summary,
                        query_results=rows, missing_fields=missing, date_options=options,
                        completed_conditions=prepared['effective_conditions'], field_sources=prepared['field_sources'],
                        readiness=prepared['readiness'], issues=[], execution={
                            'model_calls': model_count, 'tool_calls': len(calls),
                            'external_requests': run.external_request_count-initial_external_count,
                            'cache_hits': sum(bool(c.get('cache_hit')) for c in calls),
                            'failed_calls': sum(c['status'] in {'error', 'unavailable'} for c in calls),
                            'elapsed_ms': round((perf_counter()-started)*1000, 3),
                            'stop_reason': 'info_limit_or_invalid_response' if limited else None})
        result = guard_information_result({'status': 'error' if limited else 'ok', 'summary': summary,
            'travel_conditions': deepcopy(run.travel_conditions), 'domain_results': deepcopy(run.domain_results),
            'missing_fields': []}, requested or list(run.domain_results))
        if limited:
            result['status'] = 'partial' if run.domain_results else 'error'
            result['message'] = '信息获取未完成：模型响应无效或执行达到上限。'
        if not summary:
            result['summary'] = grounded_answer(result)
        return result

    def _record_tool(self, run, call, result):
        if self.memory_manager is None:
            return
        run.tool_record_seq += 1
        record_id = f"{run.turn_id}:info{run.info_executions}:record{run.tool_record_seq}:{call['id']}"
        scope = f'agent:information_query:{run.info_executions}'
        execution_id = f"{run.turn_id}:info{run.info_executions}:{call['id']}"
        metadata = next((row for row in reversed(run.tool_requests) if row['id'] == execution_id), {})
        try:
            self.memory_manager.record_tool_call([{'id': record_id, 'name': call['name'], 'arguments': call['arguments']}], run.turn_id, scope)
            self.memory_manager.record_tool_result(record_id, {**result, 'execution': deepcopy(metadata)}, run.turn_id, scope, status=result.get('status', 'error'))
        except Exception:
            # Requests and cache remain in RunState even when logging fails.
            pass
