"""Bounded main-agent decisions over durable, task-scoped travel state."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import math

from agents.workflow_contracts import task_by_id, validate_action, identifier, effective_conditions
from agents.workflow_queries import query_views
from agents.workflow_guard import check_task, check_workflow, finalize_workflow
from agents.planning_conditions import prepare_task
from context.telemetry import model_scope
from context.workflow_store import WorkflowConflictError
from travel_data.candidates import CandidateStore


@dataclass(frozen=True)
class WorkflowLimits:
    info_executions_per_task: int = 3
    main_steps_per_task: int = 4
    main_extra_steps: int = 4
    tools_per_task: int = 10
    turn_timeout: float = 600.0
    evidence_max_age_seconds: float = 3600.0

    def __post_init__(self):
        for name in ('info_executions_per_task', 'main_steps_per_task', 'main_extra_steps', 'tools_per_task'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1: raise ValueError('invalid workflow limit')
        if not math.isfinite(self.turn_timeout) or self.turn_timeout <= 0: raise ValueError('invalid workflow timeout')
        if not math.isfinite(self.evidence_max_age_seconds) or self.evidence_max_age_seconds <= 0: raise ValueError('invalid evidence age')


class WorkflowRunner:
    def __init__(self, main_agent, info_agent, memory_manager, *, limits=None, emit=None):
        self.main, self.info, self.memory = main_agent, info_agent, memory_manager
        self.limits, self.emit = limits or WorkflowLimits(), emit

    async def run(self, context, run, workflow):
        self.context, self.run_state, self.w = context, run, deepcopy(workflow)
        self.latest, self.last_message_id = None, None
        self.counts, self.seen = {}, set()
        self.main_calls = 0
        self.invalid_actions = 0
        run.workflow = self.w
        run.candidates = CandidateStore()
        run.candidates.restore(self.w.get('candidate_cache', {}))
        now = datetime.fromisoformat(context['current_time']) if context.get('current_time') else None
        if now:
            for row in self.w['results_by_query'].values():
                if row['parameters'].get('search_kind') == 'hotel_place' or not row.get('fetched_at'): continue
                if (now-datetime.fromisoformat(row['fetched_at'])).total_seconds() > self.limits.evidence_max_age_seconds:
                    row['needs_revalidation'] = True
                    from travel_data.candidates import query_cache_key
                    run.candidates.cache.pop(query_cache_key(row['domain'], row['parameters']), None)
        run.workflow_tool_budget = len(self.w['tasks']) * self.limits.tools_per_task
        try:
            return await asyncio.wait_for(self._loop(), timeout=self.limits.turn_timeout)
        except asyncio.TimeoutError:
            return self._stop('partial', 'turn_timeout', '本轮时间上限已到，已有结果已保留。')
        except WorkflowConflictError:
            return self._envelope('error', 'workflow_revision_conflict', '规划已被另一会话更新，请按最新版本继续。')
        except OSError:
            return self._envelope('error', 'workflow_save_failed', '状态保存失败，已停止；不会自动重放外部查询。')
        except Exception:
            return self._stop('error', 'workflow_execution_error', '本轮执行失败，已保留可保存的规划状态。')

    def _save(self):
        self.w['candidate_cache'] = self.run_state.candidates.snapshot()
        if self.memory:
            checkpoint = self.w.get('checkpoint')
            if checkpoint: checkpoint['expected_workflow_revision'] = self.w['revision'] + 1
            self.w = self.memory.workflow_store.save(self.w, expected_revision=self.w['revision'])
            self.memory.set_active_workflow(self.w['id'])
        else:
            self.w['revision'] += 1
            if self.w.get('checkpoint'): self.w['checkpoint']['expected_workflow_revision'] = self.w['revision']
        self.run_state.workflow = self.w

    def _message(self, payload, *, task_id=None):
        task = task_by_id(self.w, task_id) if task_id else None
        message = {**deepcopy(payload), 'schema_version': 1, 'message_id': identifier('msg'),
                   'workflow_id': self.w['id'], 'turn_id': self.run_state.turn_id, 'task_id': task_id,
                   'task_revision': task['revision'] if task else None, 'in_reply_to': self.last_message_id}
        self.last_message_id = message['message_id']
        if self.memory:
            self.memory._record_event({'turn_id': self.run_state.turn_id, 'scope': 'run', 'type': 'workflow_message',
                                       'workflow_id': self.w['id'], 'task_id': task_id, 'content': message})
        return message

    def _model_context(self):
        task = task_by_id(self.w, self.w['current_task_id'])
        effective, sources = effective_conditions(self.w['confirmed_conditions'], task['conditions'], self.run_state.effective_preferences)
        sources.update(task.get('field_sources', {}))
        index = self.w['tasks'].index(task)
        previous = self.w['tasks'][index-1] if index else None
        boundary = check_task(self.w, previous['id'], previous.get('draft_plan'))['reconstructed_plan']['schedule'] if previous else {}
        prepared = prepare_task(self.w, task['id'], self.context.get('current_time'), previous_boundary=boundary)
        effective, sources = prepared['effective_conditions'], prepared['field_sources']
        public = {k: deepcopy(v) for k, v in self.w.items() if k not in {'candidate_cache', 'results_by_query'}}
        if public.get('validation'):
            public['validation']={k:public['validation'][k] for k in ('valid','issues','validated_revisions','signature')}
        latest=deepcopy(self.latest)
        if latest:
            latest['query_results']=[{k:r.get(k) for k in ('id','task_id','task_revision','domain','result_revision','status','candidate_total','missing_fields','message')}
                                     for r in latest.get('query_results',[])]
        base={k:v for k,v in self.context.items() if k not in {'active_workflows','known_workflows'}}
        overview=[{k:t.get(k) for k in ('id','revision','status','origin','destination','purpose','requires_hotel','depends_on','summary')} for t in self.w['tasks']]
        return {**base, 'original_query': self.w['original_query'], 'current_user_query': self.context['original_query'],
                'workflow': public, 'workflow_overview': overview, 'current_task': deepcopy(task),
                'confirmed_conditions': deepcopy(self.w['confirmed_conditions']), 'effective_conditions': effective,
                'field_sources': sources, 'effective_preferences': deepcopy(self.run_state.effective_preferences),
                'preflight': prepared,
                'previous_boundary': boundary, 'relevant_results': query_views(self.w, task['id'], limit=self.run_state.limits.candidate_limit),
                'latest_child_result': latest, 'issues': deepcopy(task['issues']),
                'resources': {'main_calls_remaining': len(self.w['tasks'])*self.limits.main_steps_per_task+self.limits.main_extra_steps-self.main_calls,
                              'tools_remaining': self.run_state.workflow_tool_budget,
                              'task_info_remaining': self.limits.info_executions_per_task-self.counts.get(task['id'], 0)}}

    async def _loop(self):
        maximum = len(self.w['tasks'])*self.limits.main_steps_per_task+self.limits.main_extra_steps
        feedback = None
        for _ in range(maximum):
            self.main_calls += 1
            self.w['audit']['model_calls'] += 1
            context = self._model_context(); context['action_feedback'] = feedback
            task = context['current_task']
            self.run_state.current_task_id = task['id']
            try:
                with model_scope(turn_id=self.run_state.turn_id, workflow_id=self.w['id'], task_id=task['id'], task_revision=task['revision']):
                    raw_action = await self.main.step(context)
                self._message({'type': 'main_decision_received', 'decision': raw_action}, task_id=task['id'])
                action = validate_action(raw_action, self.w)
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                self.invalid_actions += 1
                if self.invalid_actions > 1:
                    return self._stop('error', 'invalid_workflow_action', '主 Agent 返回的动作无效，外部查询未继续。')
                feedback = {'code': 'invalid_workflow_action', 'message': str(exc)[:300],
                            'allowed_actions': ['dispatch', 'draft_task', 'ask_user', 'validate_workflow', 'finish']}
                continue
            self._message({'type': 'main_action', 'action': action}, task_id=task['id'])
            # Ignore explanatory prose when deciding whether a decision makes progress.
            progress_action = {k: v for k, v in action.items() if k not in {'summary', 'final_answer', 'reason', 'analysis', 'goal'}}
            observation = {
                'tasks': [{k: t[k] for k in ('id', 'revision', 'status', 'conditions', 'draft_plan')} for t in self.w['tasks']],
                'queries': [{k: v for k, v in r.items() if k not in {'execution', 'history'}} for r in self.w['results_by_query'].values()]}
            signature = json.dumps({'action': progress_action, 'observation': observation}, sort_keys=True, ensure_ascii=False)
            if signature in self.seen:
                return self._stop('partial', 'no_progress', '重复决策没有带来新资料，已停止本轮循环。')
            self.seen.add(signature)
            feedback = None
            name = action['action']
            premature_stop=(name=='ask_user' and not self.context.get('allow_scope_question')) or (name=='finish' and action['status']=='partial')
            if premature_stop and not task.get('query_ids') and not self.counts.get(task['id'],0) and context['preflight']['query_requests'] and self.run_state.workflow_tool_budget>0:
                feedback={'code':'available_queries_required','message':'首次缺个人条件不能跳过可执行查询；先dispatch当前段可执行领域，再保存部分草稿。',
                          'query_requests':deepcopy(context['preflight']['query_requests'])}
                continue
            if name == 'dispatch':
                if self.counts.get(task['id'], 0) >= self.limits.info_executions_per_task:
                    return self._stop('partial', 'task_information_limit', '当前段已达到资料查询上限。')
                if self.run_state.workflow_tool_budget <= 0:
                    return self._stop('partial', 'workflow_tool_limit', '本轮工具查询已达到上限。')
                self.counts[task['id']] = self.counts.get(task['id'], 0)+1
                current = task_by_id(self.w, task['id']); current['status'] = 'running'
                self._save()
                request = self._message({'type': 'task_request', 'task': {'id': task['id'], 'revision': task['revision'],
                        'mode': action.get('mode', 'query_candidates'), 'purpose': task.get('purpose'),
                        'goal': action['goal'], 'query_requests': action['query_requests'],
                        'requested_domains': list(dict.fromkeys(r['domain'] for r in action['query_requests']))},
                        'context': self._model_context()}, task_id=task['id'])
                with model_scope(turn_id=self.run_state.turn_id, workflow_id=self.w['id'], task_id=task['id'],
                                 task_revision=task['revision'], message_id=request['message_id']):
                    result = await self.info.run(request, self.run_state)
                current = task_by_id(self.w, task['id'])
                if result.get('task_id') != task['id'] or result.get('task_revision') != current['revision']:
                    self._message({'type': 'stale_task_result', 'result': result}, task_id=task['id'])
                    feedback = {'code': 'stale_task_result'}; continue
                self.latest = self._message(result, task_id=task['id'])
                execution = result.get('execution', {})
                self.w['audit']['info_executions'] += 1
                self.w['audit']['model_calls'] += execution.get('model_calls', 0)
                self.w['audit']['tool_calls'] += execution.get('tool_calls', 0)
                self.run_state.results.append({'agent_name': 'information_query', 'priority': 2,
                    'result': {'status': result['status'], 'data': result}})
                self._save()
            elif name == 'draft_task':
                current = task_by_id(self.w, task['id'])
                checked = check_task(self.w, task['id'], action['draft_plan'])
                if any(i['code'] in {'invalid_draft', 'invalid_candidate_reference', 'stale_draft', 'preserved_component_changed'} for i in checked['issues']):
                    feedback = checked; continue
                current.update(status='draft', draft_plan=deepcopy(action['draft_plan']), summary=action.get('summary', ''), issues=checked['issues'])
                for key in ('check_in', 'check_out'):
                    value = action['draft_plan'].get('schedule', {}).get(key)
                    if value and current['field_sources'].get(key) not in {'user', 'context'}:
                        current['conditions'][key] = value
                        current['field_sources'][key] = 'proposal'
                if current['status'] == 'draft':
                    pending = next((t for t in self.w['tasks'] if t['status'] == 'pending'), None)
                    if pending:
                        self.w['current_task_id'] = pending['id']
                        pending_effective, _ = effective_conditions(self.w['confirmed_conditions'], pending['conditions'], {})
                        if checked['valid'] and pending_effective.get('flexible_dates') and not pending['conditions'].get('departure_date'):
                            boundary = checked['reconstructed_plan']['schedule']['next_departure_not_before']
                            if boundary:
                                # Preserve day precision; never invent a checkout hour.
                                pending['conditions']['departure_date'] = boundary[:10]
                                pending['field_sources']['departure_date'] = 'derived'
                self._save()
            elif name == 'ask_user':
                if not self.context.get('allow_scope_question'):
                    return self._stop('partial', 'conditions_unresolved', '已保留可用结果；缺少的条件与冲突见草稿，你可以继续补充或修改。')
                for task_id in action['affected_task_ids']: task_by_id(self.w, task_id)['status'] = 'needs_input'
                self.w.update(status='needs_input', checkpoint={
                    'reason': action['reason'], 'question': action['question'], 'affected_task_ids': action['affected_task_ids'],
                    'resume_task_id': action.get('resume_task_id') or action['affected_task_ids'][0],
                    'suggested_changes': action.get('suggested_changes', [])}, stop_reason='user_input_required')
                self._save()
                return self._envelope('needs_input', 'user_input_required', action['question'])
            elif name == 'validate_workflow':
                checked = check_workflow(self.w)
                self.w['validation'] = checked
                if checked['valid']: self.w = finalize_workflow(self.w, checked)
                for current in self.w['tasks']:
                    current['issues'] = [i for i in checked['issues'] if current['id'] in i['task_ids']]
                self._save(); feedback = checked
            elif name == 'finish':
                if action['status'] == 'completed':
                    if self.w['status'] != 'completed' or not self.w.get('validation', {}).get('valid') or not check_workflow(self.w)['valid']:
                        feedback = {'code': 'global_validation_required', 'message': '所有草稿须经过当前全程校验。'}; continue
                    self._save()
                    return self._envelope('completed', 'validated', action['final_answer'])
                self.w.update(status='partial', stop_reason='model_partial')
                self._save()
                return self._envelope('partial', 'model_partial', action['final_answer'], action.get('gaps', []))
        return self._stop('partial', 'main_decision_limit', '主 Agent 决策达到本轮上限，已有草稿已保留。')

    def _stop(self, status, reason, answer):
        self.w.update(status=status, stop_reason=reason)
        try: self._save()
        except (OSError, WorkflowConflictError):
            return self._envelope('error', 'workflow_save_failed', '状态保存失败，已停止；不会自动重放外部查询。')
        return self._envelope(status, reason, answer)

    def _envelope(self, status, reason, answer, gaps=None):
        from agents.execution_harness import public_results
        checked = check_workflow(self.w)
        if reason in {'validated', 'model_partial'}:
            # As in legacy forwarding, factual prose is rebuilt from trusted evidence.
            count = len(self.w['tasks'])
            answer = (f'火车与酒店安排已整理，共 {count} 段。' if status == 'completed'
                      else f'火车与酒店规划仍有待核实信息，已保留 {count} 段任务及已有草稿。')
            budget = checked['budget']
            answer += f"已核实费用合计 {budget['known_subtotal_cny']} 元。"
            if not budget['verified']: answer += '酒店等未知费用另计，全程费用尚未完整核实。'
        simulated = any(str((row.get('source') or {}).get('provider', '')).startswith('simulation:') for row in self.w['results_by_query'].values())
        if simulated: answer = '【模拟接口测评，车次、票价和酒店均非真实数据】'+answer
        missing = sorted({field for t in self.w['tasks'] for field in prepare_task(self.w,t['id'],self.context.get('current_time'))['missing_fields']})
        return {'status': status, 'finalization_method': 'workflow', 'final_answer': answer,
                'data_mode': 'simulation' if simulated else 'provider',
                'workflow_id': self.w['id'], 'workflow_revision': self.w['revision'], 'workflow': deepcopy(self.w),
                'validated_plan': checked, 'stop_reason': reason, 'gaps': gaps or checked['issues'],
                'results': public_results(self.run_state.results), 'domain_results': {}, 'missing_fields': missing}
