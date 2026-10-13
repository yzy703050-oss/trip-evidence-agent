"""Deterministic main-agent runtime: dependencies, forwarding and shared state."""
from context.context_budget import ContextBudgetExceeded, CONTEXT_BUDGET_MESSAGE
from context.resource_budget import ModelCallLimitExceeded
import asyncio
from copy import deepcopy
import json
from sqlite3 import Error as SQLiteError

from agents.runtime import Msg
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
    """Compatible entry point. WorkflowRunner alone owns request orchestration."""
    def __init__(self, main_agent, agent_registry, memory_manager=None, emit=None):
        from agents.workflow_runner import WorkflowRunner
        self.main_agent, self.agent_registry = main_agent, agent_registry
        self.memory_manager, self.emit = memory_manager, emit
        self.runner = WorkflowRunner(main_agent, None, memory_manager, emit=emit, agent_registry=agent_registry)

    async def run_turn(self, context, run):
        self.runner.main = self.main_agent
        self.runner.memory = self.memory_manager
        return await self.runner.run_request(context, run)

    def budget_stop(self, run, exc):
        return self.runner.budget_stop(run, exc)
