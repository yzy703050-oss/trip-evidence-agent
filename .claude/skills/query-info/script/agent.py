"""Information acquisition agent with validated tools and bounded follow-up."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

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
        run.info_executions += 1
        requested = context.get('requested_domains', [])
        messages = [{'role': 'system', 'content': self.skill_loader.get_skill_content('query-info') or '根据原文整理条件，使用工具查询并总结。'},
            {'role': 'user', 'content': json.dumps({**context, 'effective_preferences': run.effective_preferences,
                'travel_conditions': run.travel_conditions, 'domain_results': run.domain_results}, ensure_ascii=False)}]
        summary, limited, tool_count = '', False, 0
        seen_ids = set()
        final_payload = {}
        for _ in range(run.limits.info_model_calls):
            try:
                with model_stage('agent:information_query'):
                    turn = await collect_model_turn(await self.model(messages, tools=self.tool_executor.schemas(), tool_choice='auto'))
                if not turn.tool_calls:
                    final_payload = robust_json_parse(turn.text)
                    summary = final_payload.get('summary', '')
                    if not isinstance(summary, str):
                        raise ValueError('summary must be text')
                    # Extras (purpose, duration) cannot replace actual queried fields.
                    extras = final_payload.get('travel_conditions', {})
                    if isinstance(extras, dict):
                        for key, value in extras.items():
                            if key not in run.travel_conditions:
                                run.travel_conditions[key] = value
                    break
                messages.append(to_assistant_tool_message(turn))
                async def execute(call, allowed):
                    if not allowed:
                        return {'status': 'error', 'message': '工具调用达到上限或 ID 重复。', 'items': []}
                    return await self.tool_executor.execute(call['name'], call['arguments'], run, call_id=call['id'])
                allowed = []
                for call in turn.tool_calls:
                    valid = call['id'] not in seen_ids and tool_count < run.limits.info_tool_calls
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
        if isinstance(selected_ids, dict) and run.candidates:
            for domain, ids in selected_ids.items():
                if domain not in {'train', 'hotel'} or not isinstance(ids, list):
                    continue
                eligible = {}
                for key, raw in run.candidates.cache.items():
                    if key.startswith(domain + ':'):
                        view = candidate_view(raw, limit=len(raw.items), offset=0,
                            constraints=run.travel_conditions.get('constraints', {}), preferences=run.effective_preferences)
                        eligible.update({item['id']: item for item in view['items']})
                if any(item_id not in eligible for item_id in ids):
                    limited = True
                    continue
                if domain in run.domain_results:
                    run.domain_results[domain]['items'] = [eligible[item_id] for item_id in dict.fromkeys(ids)][:run.limits.candidate_limit]
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
        record_id = f"{run.turn_id}:info{run.info_executions}:{len(run.tool_requests)}:{call['id']}"
        scope = f'agent:information_query:{run.info_executions}'
        try:
            self.memory_manager.record_tool_call([{'id': record_id, 'name': call['name'], 'arguments': call['arguments']}], run.turn_id, scope)
            self.memory_manager.record_tool_result(record_id, result, run.turn_id, scope, status=result.get('status', 'error'))
        except Exception:
            # Requests and cache remain in RunState even when logging fails.
            pass
