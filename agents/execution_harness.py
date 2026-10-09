"""Deterministic main-agent runtime: dependencies, forwarding and shared state."""
import asyncio
from copy import deepcopy
import json

from agentscope.message import Msg
from agents.contracts import validate_plan, validate_feedback
from agents.itinerary_module import guard_final_itinerary
from context.telemetry import model_stage
from travel_data.result_guard import guard_information_result, grounded_answer, valid_source


def merge_preference_updates(current, changes):
    result = deepcopy(current)
    if isinstance(changes, dict):
        changes = [{'type': key, 'value': value, 'action': 'replace'} for key, value in changes.items()
                   if key not in {'error', 'has_preferences'}]
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, dict) or not change.get('type') or not change.get('value'):
            continue
        key, value = change['type'], deepcopy(change['value'])
        if change.get('action') == 'append':
            old = result.get(key, [])
            values = old if isinstance(old, list) else [old] if old else []
            for item in value if isinstance(value, list) else [value]:
                if item not in values:
                    values.append(item)
            result[key] = values
        else:
            result[key] = value
    return result


def business_schedule(schedule):
    validated = validate_plan({'agent_schedule': schedule})['agent_schedule']
    rows = {}
    for row in validated:
        name = row['agent_name']
        if name not in rows:
            rows[name] = deepcopy(row)
        else:
            for key in ('depends_on', 'requested_domains'):
                rows[name][key] = list(dict.fromkeys(rows[name].get(key, []) + row.get(key, [])))
            if row.get('answer_role') != 'context':
                rows[name]['answer_role'] = 'answer'
    def waits_for_info(name, visited=None):
        visited = set() if visited is None else visited
        if name == 'information_query':
            return True
        if name in visited:
            return False
        visited.add(name)
        return any(waits_for_info(dep, visited) for dep in rows[name]['depends_on'])
    if 'information_query' in rows:
        deps = rows['information_query']['depends_on']
        deps.extend(name for name in rows if name != 'information_query' and not waits_for_info(name) and name not in deps)
    return validate_plan({'agent_schedule': list(rows.values())})['agent_schedule']


def can_forward_answer(decision, info, run):
    if decision.get('response_mode') != 'answer' or decision.get('finalization_mode') != 'forward':
        return False
    if not isinstance(info, dict) or info.get('status') != 'ok' or not isinstance(info.get('summary'), str) or not info['summary'].strip() or info.get('missing_fields') or info.get('needs_requery'):
        return False
    schedule = decision.get('agent_schedule', [])
    done = {row['agent_name'] for row in run.results}
    if not {row['agent_name'] for row in schedule} <= done:
        return False
    if any(row['agent_name'] != 'information_query' and row.get('answer_role') != 'context' for row in schedule):
        return False
    requested = next((row.get('requested_domains', []) for row in schedule if row['agent_name'] == 'information_query'), [])
    if not requested:
        return False
    guarded = guard_information_result(info, requested)
    if guarded['status'] != 'ok':
        return False
    for domain in requested:
        data = guarded['domain_results'].get(domain, {})
        if valid_source(data.get('source')) is None:
            return False
        if not data.get('items') and not (domain != 'weather' and data.get('message')):
            return False
    return True


def build_forward_result(info, run):
    return {'status': 'ok', 'finalization_method': 'forward', 'final_answer': grounded_answer(info),
        'results': public_results(run.results), 'domain_results': deepcopy(info['domain_results']),
        'travel_conditions': deepcopy(run.travel_conditions), 'missing_fields': []}


def public_results(results):
    return [{'agent_name': row['agent_name'], 'priority': row['priority'],
             'status': row['result']['status'], 'data': row['result'].get('data', {})} for row in results]


class ExecutionHarness:
    def __init__(self, main_agent, agent_registry, memory_manager=None, emit=None):
        self.main_agent, self.agent_registry = main_agent, agent_registry
        self.memory_manager, self.emit = memory_manager, emit

    async def run_turn(self, context, run):
        context = deepcopy(context)
        context['effective_preferences'] = deepcopy(run.effective_preferences)
        try:
            decision = validate_plan(await self.main_agent.plan(context))
            decision['agent_schedule'] = business_schedule(decision['agent_schedule'])
        except Exception:
            return self._error('invalid_plan', run)
        if self.memory_manager:
            self.memory_manager.session_store.append_run({'type': 'agent_plan', 'turn_id': run.turn_id,
                'agents': [{'agent_name': row['agent_name'], 'priority': row['priority']} for row in decision['agent_schedule']]})
        context.update({'rewritten_query': decision.get('rewritten_query') or context['original_query'],
                        'response_mode': decision['response_mode']})
        if decision['response_mode'] == 'direct':
            return {'status': 'ok', 'finalization_method': 'direct', 'final_answer': decision['final_answer'],
                    'results': [], 'domain_results': {}, 'missing_fields': []}
        remaining = list(decision['agent_schedule'])
        done = set()
        info = None
        while remaining:
            ready = [row for row in remaining if set(row['depends_on']) <= done]
            priority = min(row['priority'] for row in ready)
            batch = [row for row in ready if row['priority'] == priority]
            results = await asyncio.gather(*(self._execute(row, context, run) for row in batch))
            for task, result in zip(batch, results):
                run.results.append({'agent_name': task['agent_name'], 'priority': task['priority'], 'result': result})
                if task['agent_name'] == 'preference' and result['status'] == 'success':
                    run.effective_preferences = merge_preference_updates(run.effective_preferences, result['data'].get('preferences', {}))
                if task['agent_name'] == 'information_query':
                    info = result['data']
                    run.domain_results.update(info.get('domain_results', {}))
                    run.travel_conditions.update(info.get('travel_conditions', {}))
                if self.memory_manager:
                    self.memory_manager.record_agent_stage(task['agent_name'], task['priority'], result, run.turn_id)
                done.add(task['agent_name'])
                remaining.remove(task)
        if can_forward_answer(decision, info, run):
            return build_forward_result(info, run)
        return await self._finalize(context, run, info)

    async def _execute(self, task, context, run):
        name = task['agent_name']
        try:
            agent = self.agent_registry[name]
            child_context = {**deepcopy(context), 'effective_preferences': deepcopy(run.effective_preferences),
                'user_preferences': deepcopy(run.effective_preferences), 'travel_conditions': deepcopy(run.travel_conditions),
                'requested_domains': task.get('requested_domains', []), 'task_goal': task.get('expected_output', '')}
            with model_stage(f'agent:{name}'):
                if name == 'information_query' and hasattr(agent, 'run'):
                    data = await agent.run(child_context, run)
                else:
                    response = await agent.reply(Msg('Harness', json.dumps({'context': child_context,
                        'reason': task.get('reason', ''), 'expected_output': task.get('expected_output', ''),
                        'previous_results': deepcopy(run.results)}, ensure_ascii=False), 'user'))
                    data = json.loads(response.content) if isinstance(response.content, str) else response.content
            if not isinstance(data, dict):
                raise ValueError('agent returned non-object')
            status = data.get('status', 'success')
            if data.get('error') or status == 'error':
                status = 'error'
            elif status == 'ok':
                status = 'success'
            return {'status': status, 'data': data}
        except Exception:
            return {'status': 'error', 'data': {'status': 'error', 'message': '任务执行失败。'}}

    async def _finalize(self, context, run, info):
        for attempt in range(run.limits.feedback_rounds + 1):
            final = await self._final_model(context, run)
            if final.get('action') != 'needs_requery':
                return self._envelope(final, run, info)
            if attempt >= run.limits.feedback_rounds:
                return self._feedback_stop('feedback_limit', final, run)
            try:
                feedback = validate_feedback(final, run.travel_conditions)
            except (ValueError, TypeError):
                return self._feedback_stop('invalid_feedback', final, run)
            if info is None or run.info_executions >= 2:
                return self._feedback_stop('information_limit', final, run)
            run.feedback_round += 1
            task = {'agent_name': 'information_query', 'priority': 2,
                    'requested_domains': feedback['domains'], 'expected_output': feedback['reason']}
            result = await self._execute(task, {**context, 'feedback': feedback}, run)
            run.results.append({'agent_name': 'information_query', 'priority': 2, 'result': result})
            info = result['data']
            run.domain_results.update(info.get('domain_results', {}))
            if self.memory_manager:
                self.memory_manager.record_agent_stage('information_query', 2, result, run.turn_id)

    async def _final_model(self, context, run):
        final_context = {**context, 'effective_preferences': deepcopy(run.effective_preferences),
            'travel_conditions': deepcopy(run.travel_conditions), 'domain_results': deepcopy(run.domain_results),
            'results': public_results(run.results), 'feedback_round': run.feedback_round}
        try:
            return await self.main_agent.finalize(final_context)
        except Exception:
            return {'action': 'error', 'error_code': 'final_model_error'}

    def _envelope(self, final, run, info):
        if final.get('action') == 'error':
            return self._error(final['error_code'], run)
        envelope = {'status': 'ok', 'finalization_method': 'synthesize',
            'final_answer': final.get('final_answer', ''), 'results': public_results(run.results),
            'domain_results': deepcopy(run.domain_results), 'travel_conditions': deepcopy(run.travel_conditions),
            'missing_fields': final.get('missing_fields', [])}
        if final.get('action') == 'itinerary':
            envelope['itinerary'] = guard_final_itinerary(final, run.domain_results, run.travel_conditions)
            if envelope['itinerary'].get('missing_fields'):
                envelope['status'] = 'needs_input'
                envelope['missing_fields'] = envelope['itinerary']['missing_fields']
        elif final.get('action') in {'needs_input', 'needs_requery'}:
            envelope['status'] = final['action']
            envelope['feedback'] = final
        if info and info.get('status') != 'ok' and envelope['status'] == 'ok':
            envelope['status'] = info.get('status', 'partial')
        if {'train', 'hotel'} & set(run.domain_results):
            envelope['final_answer'] = grounded_answer(info or {'domain_results': run.domain_results})
        return envelope

    def _feedback_stop(self, reason, final, run):
        result = self._error(reason, run)
        result.update(status='partial', stop_reason=reason,
                      final_answer='补查已停止，现有结果未完全满足要求：' + str(final.get('reason', '')))
        return result

    @staticmethod
    def _error(code, run):
        return {'status': 'error', 'error_code': code, 'finalization_method': 'synthesize',
            'final_answer': '本轮未能完成回答，已取得的数据保留如下。',
            'results': public_results(run.results), 'domain_results': deepcopy(run.domain_results), 'missing_fields': []}
