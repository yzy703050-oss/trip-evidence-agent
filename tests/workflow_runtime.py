from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.contracts import AgentDataResult, Source
from travel_data.tools import ToolExecutor
from test_information_agent_loop import info_class
from test_workflow_queries import train_result, SOURCE
from workflow_support import proposal


class WorkflowMain:
    def __init__(self, p=None, *, finish_without_check=False):
        self.p = p or proposal(); self.actions = []; self.contexts = []; self.initial_calls = 0
        self.resume = None; self.finish_without_check = finish_without_check

    async def initialize(self, context):
        self.initial_calls += 1
        return {'response_mode': 'workflow', 'finalization_mode': 'synthesize', 'agent_schedule': [],
                **({'resume_workflow_id': self.resume['id'], 'workflow_update': self.resume['update']} if self.resume else {'workflow_proposal': self.p})}

    async def select_batch(self, context):
        # Custom action tests retain their explicit test protocol; the normal fixture
        # exercises the production batch contract without child-model calls.
        if type(self).step is not WorkflowMain.step:
            from agents.planning_conditions import prepare_task
            w = context['workflow']
            task = next((t for t in w['tasks'] if t.get('draft_stage') != 'selection' and t['status'] != 'validated'), w['tasks'][0])
            rows = [r for r in context['task_results'] if r.get('task_id') == task['id']]
            if task.get('update_scope'):
                preserved = context.get('preserved_selections', {}).get(task['id'], {})
                task['draft_plan'] = {'task_revision': task['revision'],
                    'train_selection': preserved.get('train'), 'hotel_selection': preserved.get('hotel'),
                    'schedule': {}, 'unverified_requirements': []}
            return await self.step({**context, 'current_task': task, 'relevant_results': rows,
                'preflight': prepare_task(w, task['id'], context.get('current_time')),
                'effective_conditions': {**{'passengers': 1, 'guests': 1}, **task['conditions']}})
        self.contexts.append(deepcopy(context))
        if self.finish_without_check:
            self.actions.append('finish')
            return {'action': 'finish', 'status': 'completed'}
        w = context['workflow']; drafts = []
        for task in w['tasks']:
            refs = {r['domain']: {'query_id': r['id'], 'result_revision': r['result_revision'],
                    'candidate_id': r['items'][0]['id']} for r in [r for r in context['task_results'] if r.get('task_id') == task['id']] if r['items']}
            drafts.append({'task_id': task['id'], 'task_revision': task['revision'], 'draft_plan': {
                'task_revision': task['revision'], 'train_selection': refs.get('train'), 'hotel_selection': refs.get('hotel'),
                'schedule': {k: v for k, v in task['conditions'].items() if k in {'check_in', 'check_out'}},
                'unverified_requirements': []}})
        self.actions.append('commit_batch')
        return {'action': 'commit_batch', 'expected_workflow_revision': w['revision'], 'draft_tasks': drafts}

    async def step(self, context):
        self.contexts.append(deepcopy(context)); w = context['workflow']; task = context['current_task']
        if self.finish_without_check:
            action = {'action': 'finish', 'status': 'completed', 'final_answer': '完成'}
        elif w.get('validation') and all(t.get('draft_stage') == 'selection' for t in w['tasks']):
            if w['validation']['valid']:
                action = {'action': 'finish', 'status': 'completed', 'final_answer': '交通住宿安排已整理。'}
            else:
                action = {'action': 'ask_user', 'question': '是否修改冲突日期？', 'reason': w['validation']['issues'][0]['message'],
                          'affected_task_ids': [task['id']], 'suggested_changes': [], 'resume_task_id': task['id']}
        elif task['status'] == 'pending':
            effective = context['effective_conditions']; c = task['conditions']
            queries = [{'domain': 'train', 'parameters': dict(origin=task['origin'], destination=task['destination'],
                        **({'departure_date': c['departure_date']} if c.get('departure_date') else {}), passengers=effective['passengers'])}]
            if task['requires_hotel']: queries.append({'domain': 'hotel', 'parameters': {'city': task['destination']}})
            action = {'action': 'dispatch', 'task_id': task['id'], 'goal': '查询当前段', 'query_requests': queries}
        elif task['status'] == 'draft' and task.get('draft_stage') == 'query':
            rows = context['relevant_results']; refs = {}
            for row in rows:
                if row['items']:
                    refs[row['domain']] = dict(query_id=row['id'], result_revision=row['result_revision'], candidate_id=row['items'][0]['id'])
            draft = {'task_revision': task['revision'], 'train_selection': refs.get('train'), 'hotel_selection': refs.get('hotel'),
                     'schedule': {k: v for k, v in task['conditions'].items() if k in {'check_in', 'check_out'}}, 'unverified_requirements': []}
            action = {'action': 'draft_task', 'task_id': task['id'], 'task_revision': task['revision'], 'draft_plan': draft, 'summary': '本段草稿'}
        else:
            action = {'action': 'validate_workflow', 'workflow_revision': w['revision'], 'analysis': '检查所有段'}
        self.actions.append(action['action']); return action


class OfflineProvider:
    def __init__(self, domain):
        self.domain = domain; self.hotel_places = domain == 'hotel'; self.calls = []; self.status = 'ok'; self.unknown_time = False

    async def search(self, query):
        p = query.to_dict(); self.calls.append(p)
        if self.domain == 'train':
            items = train_result({'origin': p['origin'], 'destination': p['destination'], 'conditions': p})['items']
            if self.unknown_time:
                for item in items: item['arrival_at'] = None
        else:
            items = [dict(id='place:'+p['city'], kind='hotel_place', hotel_name=p['city']+'酒店', address='地址',
                          city=p['city'], district='', location='116.1,39.1', source=SOURCE)]
        src = Source('offline', datetime.fromisoformat(SOURCE['fetched_at']), SOURCE['url'])
        return AgentDataResult(self.status, p, items if self.status == 'ok' else [], [], src, src.fetched_at, None)


class InfoModel:
    def __init__(self): self.calls = 0
    async def __call__(self, messages, **kwargs):
        self.calls += 1
        if len(messages) == 2:
            context = json.loads(messages[1]['content']); requests = context['task']['query_requests']
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': f'tool{i}', 'name': r['domain']+'_search',
                                             'input': r['parameters']} for i, r in enumerate(requests)])
        return SimpleNamespace(text='{"summary":"当前段资料已整理"}')


class Runtime:
    def __init__(self, tmp_path, *, main=None, session='s'):
        self.memory = MemoryManager('alice', session, storage_path=str(tmp_path))
        self.main = main or WorkflowMain(); self.info_model = InfoModel()
        self.train = OfflineProvider('train'); self.hotel = OfflineProvider('hotel')
        model = MeteredModel(self.info_model, self.memory.session_store.append_run)
        self.info = info_class()(model=model, tool_executor=ToolExecutor({'train_search': self.train, 'hotel_search': self.hotel}), memory_manager=self.memory)
        class Registry(dict):
            pass
        registry = Registry(information_query=self.info)
        registry.providers = {'train_search': self.train, 'hotel_search': self.hotel}
        self.harness = ExecutionHarness(self.main, registry, self.memory)

    async def turn(self, query='安排火车酒店'):
        self.harness.runner.executor = self.info.tool_executor  # Explicit provider injection, no child execution.
        run = RunState(self.memory.start_turn(query))
        result = await self.harness.run_turn({'original_query': query, 'current_time': '2026-10-10T10:00:00+08:00'}, run)
        return result
