"""Validated business decisions and state owned by one execution turn."""
from copy import deepcopy
from dataclasses import dataclass, field
import math
from decimal import Decimal
import re

AGENTS = {'preference', 'memory_query', 'rag_knowledge', 'information_query'}
INTENT_TYPES = frozenset({'ask', 'plan', 'update', 'control'})
DOMAINS = {'train', 'hotel', 'guide', 'weather', 'web'}
FILTER_KEYS = {'hotel_max_total_cny', 'hotel_max_nightly_cny', 'train_max_total_cny',
               'seat_class', 'departure_time_after', 'departure_time_before', 'available_only'}


def validate_constraints(value):
    if not isinstance(value, dict) or set(value) - FILTER_KEYS:
        raise ValueError('unsupported constraints')
    for key, val in value.items():
        if key.endswith('_cny'):
            amount = Decimal(str(val))
            if not amount.is_finite() or amount < 0:
                raise ValueError('invalid budget')
        elif key == 'available_only':
            if type(val) is not bool:
                raise ValueError('invalid inventory constraint')
        elif not isinstance(val, str) or not val.strip():
            raise ValueError('invalid constraint')
        elif key.startswith('departure_time_') and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', val):
            raise ValueError('invalid time')
    return value


@dataclass(frozen=True)
class RunLimits:
    info_model_calls: int = 6
    info_tool_calls: int = 10
    feedback_rounds: int = 1
    tool_timeout: float = 30.0
    candidate_limit: int = 5

    def __post_init__(self):
        for name in ('info_model_calls', 'info_tool_calls', 'candidate_limit'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError('limits must be positive integers')
        if type(self.feedback_rounds) is not int or not 0 <= self.feedback_rounds <= 1:
            raise ValueError('at most one feedback round')
        if not math.isfinite(self.tool_timeout) or self.tool_timeout <= 0:
            raise ValueError('invalid timeout')


@dataclass
class RunState:
    turn_id: str
    limits: RunLimits = field(default_factory=RunLimits)
    effective_preferences: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    domain_results: dict = field(default_factory=dict)
    travel_conditions: dict = field(default_factory=dict)
    feedback_round: int = 0
    tool_requests: list = field(default_factory=list)
    external_requests_started: bool = False
    external_request_count: int = 0
    external_requests_by_task: dict = field(default_factory=dict)
    candidates: object = None
    info_executions: int = 0
    tool_record_seq: int = 0
    workflow: dict | None = None
    current_task_id: str | None = None
    query_records: dict = field(default_factory=dict)
    workflow_tool_budget: int | None = None


def normalize_intents(plan: dict) -> list[dict]:
    """Keep purposes small; sources and executable actions live in their own fields."""
    intents = plan.get('intents', [])
    if not isinstance(intents, list):
        raise ValueError('intents must be a list')
    if intents:
        kinds = []
        for item in intents:
            if not isinstance(item, dict) or not isinstance(item.get('type'), str) or item['type'] not in INTENT_TYPES:
                raise ValueError('invalid intent type')
            kinds.append(item['type'])
    else:
        kinds = []
        names = {row['agent_name'] for row in plan.get('agent_schedule', [])}
        if 'preference' in names:
            kinds.append('update')
        update = plan.get('travel_update')
        if plan.get('feedback_scope'):
            kinds.append('update')
        elif update:
            kinds.append('control' if update['update_type'] in {'adopt', 'pause', 'cancel'} else 'update')
        elif plan.get('workflow_proposal') is not None or plan['response_mode'] == 'itinerary':
            kinds.append('plan')
        elif plan.get('resume_workflow_id'):
            legacy = plan.get('workflow_update') or {}
            kinds.append('update' if legacy.get('confirmed_conditions') or legacy.get('task_updates') else 'control')
        elif names - {'preference'} or not kinds:
            kinds.append('ask')
    return [{'type': kind} for kind in dict.fromkeys(kinds)]


def validate_plan(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('plan must be an object')
    plan = deepcopy(value)
    if isinstance(plan.get('travel_update'),dict):
        target=plan['travel_update'].get('target',{})
        if isinstance(target,dict) and isinstance(target.get('workflow_id'),str):
            plan.setdefault('resume_workflow_id',target['workflow_id'])
            plan['response_mode']='workflow'
    mode = plan.setdefault('response_mode', 'answer')
    if mode == 'itinerary' and (isinstance(plan.get('workflow_proposal'), dict) or isinstance(plan.get('resume_workflow_id'), str)):
        # A structured task proposal is an unambiguous workflow signal, even with the legacy label.
        mode = plan['response_mode'] = 'workflow'
    finish = plan.setdefault('finalization_mode', 'synthesize')
    if mode not in {'direct', 'answer', 'itinerary', 'workflow'} or finish not in {'forward', 'synthesize'}:
        raise ValueError('invalid response mode')
    if finish == 'forward' and mode != 'answer':
        raise ValueError('only information answers can be forwarded')
    rows = plan.setdefault('agent_schedule', [])
    if mode == 'workflow':
        if not isinstance(plan.get('workflow_proposal'), dict) and not isinstance(plan.get('resume_workflow_id'), str):
            raise ValueError('workflow needs a proposal or resume target')
        if any(isinstance(r, dict) and r.get('agent_name') == 'information_query' for r in rows):
            raise ValueError('workflow queries are task-scoped, not preflight agents')
        update=plan.get('travel_update')
        if update is not None:
            if not isinstance(update,dict) or update.get('update_type') not in {'supplement','change','regenerate','replace','change_route','adopt','pause','cancel'}:
                raise ValueError('invalid travel update')
            target=update.get('target',{})
            if not isinstance(target,dict) or target.get('workflow_id')!=plan.get('resume_workflow_id'):
                raise ValueError('update target differs from resume target')
            if update['update_type'] == 'change' and not update.get('condition_updates'):
                raise ValueError('change needs explicit changed conditions; unclear dissatisfaction requires a direct feedback_scope question, not empty change')
    if plan.get('feedback_scope') is not None:
        scope=plan['feedback_scope']
        if mode!='direct' or not isinstance(scope,dict) or not all(isinstance(scope.get(k),str) and scope[k].strip() for k in ('workflow_id','question')):
            raise ValueError('invalid feedback scope checkpoint')
    if not isinstance(rows, list) or (mode == 'direct' and rows):
        raise ValueError('invalid schedule')
    names = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('agent_name') not in AGENTS:
            raise ValueError('unknown business agent')
        name = row['agent_name']
        names.add(name)
        priority = row.get('priority', 2 if name == 'information_query' else 1)
        if isinstance(priority, bool) or not isinstance(priority, (int, float)) or not math.isfinite(priority):
            raise ValueError('invalid priority')
        row['priority'] = 2 if name == 'information_query' else 1
        deps = row.setdefault('depends_on', [])
        if not isinstance(deps, list) or any(dep not in AGENTS or dep == name for dep in deps):
            raise ValueError('invalid dependency')
        if name == 'information_query':
            domains = row.setdefault('requested_domains', [])
            if not isinstance(domains, list) or any(d not in DOMAINS for d in domains):
                raise ValueError('invalid query domain')
    edges = {name: set() for name in names}
    for row in rows:
        if not set(row['depends_on']) <= names:
            raise ValueError('missing dependency')
        edges[row['agent_name']].update(row['depends_on'])
    done = set()
    while len(done) < len(edges):
        ready = {name for name, deps in edges.items() if name not in done and deps <= done}
        if not ready:
            raise ValueError('dependency cycle')
        done.update(ready)
    if mode == 'direct' and not isinstance(plan.get('final_answer'), str):
        raise ValueError('direct response needs an answer')
    plan['intents'] = normalize_intents(plan)
    return plan


def validate_final(value: dict) -> dict:
    if not isinstance(value, dict) or value.get('action') not in {'answer', 'itinerary', 'needs_input', 'needs_requery'}:
        raise ValueError('invalid final action')
    if value['action'] == 'answer' and not isinstance(value.get('final_answer'), str):
        raise ValueError('answer must be text')
    if value['action'] == 'needs_requery':
        if not value.get('reason') or not isinstance(value.get('domains'), list) or not value['domains'] or any(d not in DOMAINS for d in value['domains']):
            raise ValueError('invalid feedback')
        if not isinstance(value.get('constraints', {}), dict):
            raise ValueError('invalid constraints')
    return deepcopy(value)


def validate_feedback(value: dict, conditions: dict) -> dict:
    feedback = validate_final(value)
    if feedback['action'] != 'needs_requery':
        raise ValueError('not feedback')
    constraints = feedback.get('constraints', {})
    if not constraints:
        raise ValueError('no new query basis')
    validate_constraints({k: v for k, v in constraints.items() if k not in {'candidate_offset', 'refresh'}})
    if 'candidate_offset' in constraints and (type(constraints['candidate_offset']) is not int or constraints['candidate_offset'] < 0):
        raise ValueError('invalid window')
    if 'refresh' in constraints and constraints['refresh'] is not True:
        raise ValueError('no refresh basis')
    confirmed = {**conditions, **conditions.get('constraints', {})}
    if any(key in confirmed and confirmed[key] != val for key, val in constraints.items()):
        raise ValueError('cannot change confirmed conditions')
    if all(key in confirmed for key in constraints):
        raise ValueError('no new query basis')
    if conditions.get('missing_fields'):
        raise ValueError('missing user conditions')
    return feedback
