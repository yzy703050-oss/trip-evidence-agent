"""Deterministic main-agent runtime: dependencies, forwarding and shared state."""
from context.context_budget import ContextBudgetExceeded, CONTEXT_BUDGET_MESSAGE
from context.resource_budget import ModelCallLimitExceeded
import asyncio
from copy import deepcopy
import json
from sqlite3 import Error as SQLiteError

from agentscope.message import Msg
from agents.contracts import validate_plan, validate_feedback
from agents.itinerary_module import guard_final_itinerary
from context.telemetry import model_stage, model_scope
from travel_data.result_guard import guard_information_result, grounded_answer, valid_source
from utils.response_renderer import finalize_business_result
from agents.context_projection import project_delegation_context


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
        from context.resource_budget import success_budget_scope
        from config import WORKFLOW_LIMITS
        with success_budget_scope(main_per_turn=WORKFLOW_LIMITS.get('main_model_calls_per_turn'),
                                  main_per_task=WORKFLOW_LIMITS.get('main_model_calls_per_task')):
            return await self._run_with_limits(context, run)

    async def _run_with_limits(self, context, run):
        from config import WORKFLOW_LIMITS
        from context.resource_budget import ModelCallLimitExceeded
        try:
            result = await asyncio.wait_for(self._run_turn(context,run),timeout=WORKFLOW_LIMITS['turn_timeout'])
        except ContextBudgetExceeded as exc:
            return self.budget_stop(run, exc)
        except ModelCallLimitExceeded:
            return finalize_business_result({**self._error('main_model_call_limit', run), 'status': 'partial',
                                             'stop_reason': 'main_model_call_limit', 'final_answer': '本轮模型请求达到上限，已有资料已保留。'})
        except asyncio.TimeoutError:
            result={**self._error('turn_timeout',run),'status':'partial','stop_reason':'turn_timeout',
                    'final_answer':'本轮时间上限已到，已有结果已保留，可以继续修改。'}
            if run.workflow is not None and self.memory_manager:
                from context.workflow_store import WorkflowConflictError
                w=deepcopy(run.workflow); w.update(status='partial',stop_reason='turn_timeout')
                try: w=self.memory_manager.workflow_store.save(w,expected_revision=w['revision'])
                except (OSError,SQLiteError,WorkflowConflictError): return finalize_business_result(self._error('workflow_save_failed',run))
                result.update(finalization_method='workflow',workflow=w,workflow_id=w['id'],workflow_revision=w['revision'])
        result = finalize_business_result(result)
        if (result.get('workflow') and result.get('validated_plan')
                and result.get('status') in {'completed', 'partial'}):
            from utils.workflow_reply import build_reply_facts, render_reply_template
            facts = build_reply_facts(result)
            result['reply_style'] = 'fallback'
            compose = getattr(self.main_agent, 'compose_workflow_reply', None)
            if compose is not None and result.get('stop_reason') != 'turn_timeout':
                try:
                    with model_scope(turn_id=run.turn_id, workflow_id=result.get('workflow_id')):
                        reply = await asyncio.wait_for(compose({
                            'original_query': context['original_query'],
                            'current_time': context.get('current_time'),
                            'status': result['status'],
                            'reply_facts': {'values': facts['values'], 'required': facts['required']}
                        }), timeout=15.0)
                    result['final_answer'] = render_reply_template(reply['template'], facts)
                    result['reply_style'] = 'model'
                except ContextBudgetExceeded as exc:
                    return self.budget_stop(run, exc)
                except Exception:
                    result['final_answer'] = facts['fallback']
        return result

    def budget_stop(self, run, exc):
        """Finish the user turn without any business-model fallback."""
        result = {**self._error('context_budget_exceeded', run), 'status': 'partial',
                  'stop_reason': 'context_budget_exceeded', 'final_answer': CONTEXT_BUDGET_MESSAGE,
                  'budget': {'agent_scope': exc.agent_scope, 'phase': exc.phase,
                             'tokens': exc.estimate.tokens, 'method': exc.estimate.method,
                             'estimated': exc.estimate.estimated, 'budget_tokens': exc.budget_tokens}}
        if run.workflow is not None:
            from context.workflow_store import WorkflowConflictError
            w = deepcopy(run.workflow)
            if w.get('stop_reason') != 'context_budget_exceeded':
                w.update(status='partial', stop_reason='context_budget_exceeded')
                if run.candidates is not None:
                    w['candidate_cache'] = run.candidates.snapshot()
                if self.memory_manager:
                    try:
                        w = self.memory_manager.workflow_store.save(w, expected_revision=w['revision'])
                    except (OSError, SQLiteError, WorkflowConflictError):
                        result['state_save_failed'] = True
            result.update(workflow=w, workflow_id=w['id'], workflow_revision=w['revision'],
                          finalization_method='workflow')
        self._persist(result, run)
        return result

    async def _run_turn(self, context, run):
        context = deepcopy(context)
        context.setdefault('default_round_trip', True)
        context['effective_preferences'] = deepcopy(run.effective_preferences)
        from agents.planning_conditions import ambiguous_date_options
        context['ambiguous_date_options']=ambiguous_date_options(context['original_query'],context.get('current_time'))
        try:
            if self.memory_manager:
                summaries = self.memory_manager.workflow_store.list_summaries()
                active_ids = set(self.memory_manager.session_store.read_state().get('active_workflow_ids', []))
                context['active_workflows'] = [deepcopy(w) for w in summaries if w['id'] in active_ids]
                context['known_workflows'] = self.memory_manager.get_workflow_context(query=context['original_query'])
                pending = self.memory_manager.session_store.read_state().get('pending_workflow_match')
                if pending:
                    context['pending_workflow_match'] = pending
                    known_ids = {w['id'] for w in context['known_workflows']}
                    for candidate in pending['candidates']:
                        w = next((w for w in summaries if w['id'] == candidate['id']), None)
                        if w and w['id'] not in known_ids:
                            context['known_workflows'].append(deepcopy(w))
                            known_ids.add(w['id'])
            initial = getattr(self.main_agent, 'initialize', self.main_agent.plan if hasattr(self.main_agent, 'plan') else None)
            with model_scope(turn_id=run.turn_id):
                decision = validate_plan(await initial(context))
                if decision['response_mode'] == 'workflow' and not decision.get('resume_workflow_id'):
                    from agents.workflow_contracts import create_workflow
                    try:
                        create_workflow(decision['workflow_proposal'], context, require_stay_proposals=True)
                    except (ValueError, TypeError, ArithmeticError) as exc:
                        repair = getattr(self.main_agent, 'repair_proposal', None)
                        if repair is None: raise
                        decision['workflow_proposal'] = await repair(context, decision['workflow_proposal'], str(exc))
                        create_workflow(decision['workflow_proposal'], context, require_stay_proposals=True)
            decision['agent_schedule'] = business_schedule(decision['agent_schedule'])
            if (context.get('pending_workflow_match') and decision.get('resume_workflow_id') in
                    {w['id'] for w in context['pending_workflow_match']['candidates']}):
                decision['reuse_saved_workflow'] = True
        except (ContextBudgetExceeded, ModelCallLimitExceeded):
            raise
        except Exception:
            return self._error('invalid_plan', run)
        decision, match_result = await self._match_new_workflow(decision, context, run)
        if match_result is not None:
            return match_result
        if self.memory_manager:
            self.memory_manager.session_store.append_run({'type': 'agent_plan', 'turn_id': run.turn_id,
                'decision': deepcopy(decision),
                'agents': [{'agent_name': row['agent_name'], 'priority': row['priority']} for row in decision['agent_schedule']]})
        context.update({'rewritten_query': decision.get('rewritten_query') or context['original_query'],
                        'response_mode': decision['response_mode']})
        if decision['response_mode'] == 'direct':
            scope=decision.get('feedback_scope')
            if scope and self.memory_manager:
                from context.workflow_store import WorkflowConflictError
                w=self.memory_manager.workflow_store.load(scope['workflow_id'])
                observed=next((r for r in context.get('known_workflows',[]) if r['id']==scope['workflow_id']),None)
                if not w or not observed or observed['revision']!=w['revision']:
                    return self._error('workflow_revision_conflict',run)
                w.update(status='needs_input',checkpoint=dict(kind='feedback_scope',question=scope['question'],expected_workflow_revision=w['revision']+1),stop_reason='feedback_scope_required')
                try: w=self.memory_manager.workflow_store.save(w,expected_revision=w['revision'])
                except (WorkflowConflictError,OSError,SQLiteError): return self._error('workflow_save_failed',run)
                return dict(status='needs_input',finalization_method='direct',final_answer=scope['question'],workflow_id=w['id'],workflow=w,results=[],domain_results={},missing_fields=[])
            return {'status': 'ok', 'finalization_method': 'direct', 'final_answer': decision['final_answer'],
                    'results': [], 'domain_results': {}, 'missing_fields': []}
        remaining = list(decision['agent_schedule'])
        done = set()
        info = None
        while remaining:
            ready = [row for row in remaining if set(row['depends_on']) <= done]
            priority = min(row['priority'] for row in ready)
            batch = [row for row in ready if row['priority'] == priority]
            results = await asyncio.gather(*(self._execute(row, context, run) for row in batch), return_exceptions=True)
            stopped = next((r for r in results if isinstance(r, ContextBudgetExceeded)), None)
            if stopped is not None:
                for task, result in zip(batch, results):
                    if isinstance(result, dict):
                        run.results.append({'agent_name': task['agent_name'], 'priority': task['priority'], 'result': result})
                raise stopped
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            for task, result in zip(batch, results):
                run.results.append({'agent_name': task['agent_name'], 'priority': task['priority'],
                                    'answer_role': task.get('answer_role', 'answer'), 'result': result})
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
        if decision['response_mode'] == 'workflow':
            result = await self._run_workflow(decision, context, run)
            if result.get('workflow_id') and result.get('status') != 'error':
                self._save_match_question(None)
        elif can_forward_answer(decision, info, run):
            result = build_forward_result(info, run)
        else:
            result = await self._finalize(context, run, info)
        self._persist(result, run)
        if {'train', 'hotel', 'weather', 'guide'} & set(run.domain_results):
            confirmations = []
            for row in run.results:
                if row['agent_name'] != 'preference' or row['result']['status'] != 'success':
                    continue
                updates = merge_preference_updates({}, row['result']['data'].get('preferences', {}))
                for key in updates:
                    value = run.effective_preferences.get(key)
                    label = {'hotel_brands': '酒店品牌偏好', 'seat_preference': '席别偏好'}.get(key, '出行偏好')
                    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    confirmations.append(f'{label}已设置为：{rendered}。')
            if confirmations:
                result['final_answer'] = '\n'.join(confirmations + [result['final_answer']])
        return result

    def _save_match_question(self, pending):
        if self.memory_manager:
            store = self.memory_manager.session_store
            if pending is None:
                store.update_state({}, remove_keys=('pending_workflow_match',))
            else:
                store.update_state({'pending_workflow_match': pending})

    async def _match_new_workflow(self, decision, context, run):
        """Do not save a new workflow until the model has compared user-owned trips."""
        matcher = getattr(self.main_agent, 'match_workflow', None)
        if not self.memory_manager or not matcher or decision['response_mode'] != 'workflow' or decision.get('resume_workflow_id'):
            return decision, None
        from agents.workflow_matching import workflow_identity, validate_match
        from agents.workflow_contracts import create_workflow
        from agents.planning_conditions import prepare_task
        from agents.workflow_guard import check_workflow
        try:
            workflows = self.memory_manager.workflow_store.list_summaries()
            if not workflows:
                return decision, None
            proposed = create_workflow(decision['workflow_proposal'], context, require_stay_proposals=True)
            # Include program-owned defaults but retain their source for the model.
            prepare_task(proposed, proposed['tasks'][0]['id'], context.get('current_time'))
            match_context = {
                'original_query': context['original_query'], 'current_time': context.get('current_time'),
                'proposal': workflow_identity(proposed),
                'candidates': [workflow_identity(w) for w in workflows],
            }
            with model_scope(turn_id=run.turn_id):
                match = validate_match(await matcher(match_context), match_context['candidates'])
            self.memory_manager.session_store.append_run({'type': 'workflow_match', 'turn_id': run.turn_id,
                                                         'decision': deepcopy(match)})
            if match['action'] == 'clarify':
                self._save_match_question({'question': match['question'], 'original_query': context['original_query'],
                                          'proposal': deepcopy(decision['workflow_proposal']),
                                          'candidates': match_context['candidates']})
                return decision, dict(status='needs_input', finalization_method='direct',
                    final_answer=match['question'], stop_reason='workflow_match_required',
                    results=[], domain_results={}, missing_fields=[])
            if match['action'] == 'reuse':
                selected = next(w for w in workflows if w['id'] == match['workflow_id'])
                context['known_workflows'].append(deepcopy(selected))
                decision = deepcopy(decision)
                decision.pop('workflow_proposal', None)
                decision['resume_workflow_id'] = selected['id']
                decision['reuse_saved_workflow'] = True
                if match.get('refresh'):
                    decision['travel_update'] = {'update_type': 'regenerate',
                        'target': {'workflow_id': selected['id'], 'task_ids': [t['id'] for t in selected['tasks']],
                                   'components': ['train', 'hotel']}}
            return decision, None
        except (ContextBudgetExceeded, ModelCallLimitExceeded):
            raise
        except Exception:
            return decision, self._error('invalid_workflow_match', run)

    async def _run_workflow(self, decision, context, run):
        from agents.workflow_contracts import create_workflow, apply_user_update
        from agents.workflow_guard import check_workflow
        from agents.workflow_runner import WorkflowRunner, WorkflowLimits
        from context.workflow_store import WorkflowConflictError
        try:
            workflow_id = decision.get('resume_workflow_id')
            if workflow_id:
                if self.memory_manager is None: raise ValueError('resume needs storage')
                workflow = self.memory_manager.workflow_store.load(workflow_id)
                if workflow is None: raise ValueError('unknown resume workflow')
                observed = next((w for w in context.get('known_workflows', context.get('active_workflows', [])) if w['id'] == workflow_id), None)
                if observed is None or observed['revision'] != workflow['revision']:
                    raise WorkflowConflictError('resume decision was made against a different revision')
                checkpoint = workflow.get('checkpoint')
                if checkpoint and checkpoint.get('expected_workflow_revision') != workflow['revision']:
                    raise WorkflowConflictError('checkpoint revision changed')
                if (decision.get('reuse_saved_workflow') and workflow['status'] == 'completed'
                        and not decision.get('travel_update') and not decision.get('workflow_update')):
                    checked = check_workflow(workflow)
                    if not checked['valid']:
                        raise ValueError('saved workflow validation is no longer valid')
                    self.memory_manager.set_active_workflow(workflow_id)
                    run.workflow = workflow
                    simulated = any(str((row.get('source') or {}).get('provider', '')).startswith('simulation:')
                                    for row in workflow['results_by_query'].values())
                    answer = '已找到这趟旅行的已有规划，沿用原方案。以下为此前查询的结果，票价和余票并非实时信息。'
                    if simulated:
                        answer = '【模拟接口测评，车次、票价和酒店均非真实数据】' + answer
                    return dict(status='completed', finalization_method='workflow',
                        final_answer=answer, data_mode='simulation' if simulated else 'provider',
                        workflow_id=workflow_id, workflow_revision=workflow['revision'], workflow=deepcopy(workflow),
                        validated_plan=checked, stop_reason='workflow_reused', gaps=checked['issues'],
                        results=[], domain_results={}, missing_fields=[])
                if decision.get('travel_update'):
                    from agents.travel_updates import apply_travel_update
                    from agents.workflow_contracts import validate_quote_requirement
                    validate_quote_requirement(decision['travel_update'].get('condition_updates', {}), context)
                    workflow = apply_travel_update(workflow,decision['travel_update'], context=context)
                    if decision.get('reuse_saved_workflow') and decision['travel_update']['update_type'] == 'regenerate':
                        # Explicit latest-data requests must reach providers, even for a warm cache.
                        workflow['candidate_cache'] = {}
                        for row in workflow['results_by_query'].values():
                            row['needs_revalidation'] = True
                else:
                    from agents.workflow_contracts import validate_quote_requirement
                    validate_quote_requirement(decision.get('workflow_update', {}).get('confirmed_conditions', {}), context)
                    for update in decision.get('workflow_update', {}).get('task_updates', []):
                        validate_quote_requirement(update.get('conditions', {}), context)
                    workflow = apply_user_update(workflow, decision.get('workflow_update', {}))
                workflow = self.memory_manager.workflow_store.save(workflow, expected_revision=workflow['revision'])
            else:
                workflow = create_workflow(decision['workflow_proposal'], context, require_stay_proposals=True)
                if self.memory_manager:
                    workflow = self.memory_manager.workflow_store.save(workflow, expected_revision=None)
            workflow['effective_preferences'] = deepcopy(run.effective_preferences)
            if workflow['status'] in {'paused','cancelled'}:
                return dict(status=workflow['status'],workflow_id=workflow['id'],workflow_revision=workflow['revision'],workflow=workflow,
                            finalization_method='workflow',final_answer='已停止规划并保存方案。没有执行购票、订房或退改签。',results=[],domain_results={},missing_fields=[])
            from config import WORKFLOW_LIMITS
            runner = WorkflowRunner(self.main_agent, self.agent_registry['information_query'], self.memory_manager,
                                    limits=WorkflowLimits(**WORKFLOW_LIMITS), emit=self.emit)
            return await runner.run(context, run, workflow)
        except WorkflowConflictError:
            return {**self._error('workflow_revision_conflict', run), 'stop_reason': 'workflow_revision_conflict'}
        except (OSError, SQLiteError):
            return {**self._error('workflow_save_failed', run), 'stop_reason': 'workflow_save_failed'}
        except (ValueError, TypeError, KeyError, ArithmeticError):
            return {**self._error('invalid_workflow_proposal', run), 'stop_reason': 'invalid_workflow_proposal'}

    @staticmethod
    def _child_previous_results(results):
        from agents.context_projection import project_child_result
        return [{**{k: deepcopy(v) for k,v in row.items() if k != 'result'},
                 'result': {**{k: deepcopy(v) for k,v in row['result'].items() if k != 'data'},
                            'data': project_child_result(row['result'].get('data', {}))}}
                for row in results]

    async def _execute(self, task, context, run):
        name = task['agent_name']
        try:
            agent = self.agent_registry[name]
            child_context = {**project_delegation_context(context), 'effective_preferences': deepcopy(run.effective_preferences),
                'user_preferences': deepcopy(run.effective_preferences), 'travel_conditions': deepcopy(run.travel_conditions),
                'previous_results': self._child_previous_results(run.results),
                'requested_domains': task.get('requested_domains', []), 'task_goal': task.get('expected_output', '')}
            if self.memory_manager:
                latest = self.memory_manager.get_compacted_context()['summary']
                child_context['session_summary'] = latest or child_context.get('session_summary', '')
            with model_scope(turn_id=run.turn_id), model_stage(f'agent:{name}'):
                if name == 'information_query' and hasattr(agent, 'run'):
                    data = await agent.run(child_context, run)
                else:
                    response = await agent.reply(Msg('Harness', json.dumps({'context': child_context,
                        'reason': task.get('reason', ''), 'expected_output': task.get('expected_output', ''),
                        'previous_results': self._child_previous_results(run.results)}, ensure_ascii=False), 'user'))
                    data = json.loads(response.content) if isinstance(response.content, str) else response.content
            if not isinstance(data, dict):
                raise ValueError('agent returned non-object')
            status = data.get('status', 'success')
            if data.get('error') or status == 'error':
                status = 'error'
            elif status == 'ok':
                status = 'success'
            return {'status': status, 'data': data}
        except (ContextBudgetExceeded, ModelCallLimitExceeded):
            raise
        except Exception:
            return {'status': 'error', 'data': {'status': 'error', 'message': '任务执行失败。'}}

    async def _finalize(self, context, run, info):
        for attempt in range(run.limits.feedback_rounds + 1):
            final = await self._final_model(context, run)
            if final.get('action') != 'needs_requery':
                result = self._envelope(final, run, info)
                self._record_finalize(result, run)
                return result
            self._record_finalize({'status': 'needs_requery', 'feedback': final}, run)
            if attempt >= run.limits.feedback_rounds:
                return self._feedback_stop('feedback_limit', final, run)
            try:
                feedback = validate_feedback(final, run.travel_conditions)
            except (ValueError, TypeError, ArithmeticError):
                return self._feedback_stop('invalid_feedback', final, run)
            from context.resource_budget import is_calibrating
            if info is None or (not is_calibrating() and run.successful_info_executions >= run.limits.info_executions_per_run):
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
            with model_scope(turn_id=run.turn_id):
                return await self.main_agent.finalize(final_context)
        except (ContextBudgetExceeded, ModelCallLimitExceeded):
            raise
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
            envelope['final_answer'] = '行程安排建议如下，未知费用和未核实事项见说明。'
            envelope['itinerary'] = guard_final_itinerary(final, run.domain_results, run.travel_conditions)
            if envelope['itinerary'].get('missing_fields'):
                envelope['status'] = 'needs_input'
                envelope['missing_fields'] = envelope['itinerary']['missing_fields']
        elif final.get('action') in {'needs_input', 'needs_requery'}:
            envelope['status'] = final['action']
            envelope['feedback'] = final
        if info and info.get('status') != 'ok' and envelope['status'] == 'ok':
            envelope['status'] = info.get('status', 'partial')
        if final.get('action') != 'itinerary' and {'train', 'hotel', 'weather', 'guide'} & set(run.domain_results):
            answers = [grounded_answer({'domain_results': run.domain_results})]
            for row in run.results:
                if row['agent_name'] in {'rag_knowledge', 'memory_query'} and row.get('answer_role') != 'context':
                    data = row['result']['data']
                    if isinstance(data.get('answer'), str) and not data.get('error'):
                        answers.append(data['answer'])
            envelope['final_answer'] = '\n'.join(answers)
        return envelope

    def _feedback_stop(self, reason, final, run):
        result = self._error(reason, run)
        explanations = {'feedback_limit': '已达到本轮补查上限。',
                        'invalid_feedback': '缺少可执行补查条件，或需要改变已确认的要求。',
                        'information_limit': '本轮信息获取已达到上限或未安排该任务。'}
        result.update(status='partial', stop_reason=reason,
                      final_answer='补查已停止，现有结果未完全满足要求。' + explanations[reason]
                                   + '\n' + grounded_answer({'domain_results': run.domain_results}))
        return result

    def _record_finalize(self, result, run):
        if self.memory_manager:
            self.memory_manager.record_agent_stage('main_finalize', 3,
                {'status': 'success' if result['status'] == 'ok' else result['status'], 'data': result}, run.turn_id)

    def _persist(self, result, run):
        if self.memory_manager is None:
            return
        memory = self.memory_manager.long_term
        old = memory.get_preference()
        for key, value in run.effective_preferences.items():
            if value != old.get(key):
                memory.save_preference(key, value)
        plan = result.get('itinerary', {})
        # WorkflowRunner has already saved the canonical plan; history reads it directly.
        if plan.get('planning_complete') is True and result['status'] in {'ok', 'partial'}:
            conditions = run.travel_conditions
            memory.save_trip_history({
                'origin': conditions.get('origin'), 'destination': conditions.get('destination', conditions.get('city')),
                'start_date': conditions.get('start_date', conditions.get('departure_date', conditions.get('check_in'))),
                'end_date': conditions.get('end_date', conditions.get('check_out')),
                'purpose': conditions.get('purpose'), 'travel_conditions': deepcopy(conditions),
                'itinerary': deepcopy(plan), 'turn_id': run.turn_id})

    @staticmethod
    def _error(code, run):
        return {'status': 'error', 'error_code': code, 'finalization_method': 'synthesize',
            'final_answer': '本轮未能完成回答，已取得的数据保留如下。',
            'results': public_results(run.results), 'domain_results': deepcopy(run.domain_results), 'missing_fields': []}
