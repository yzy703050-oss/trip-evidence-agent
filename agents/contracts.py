"""Validated business decisions and state owned by one execution turn."""
from copy import deepcopy
from dataclasses import dataclass, field
import math

AGENTS = {'preference', 'memory_query', 'rag_knowledge', 'information_query'}
DOMAINS = {'train', 'hotel', 'guide', 'weather', 'web'}


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
    candidates: object = None
    info_executions: int = 0


def validate_plan(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError('plan must be an object')
    plan = deepcopy(value)
    mode = plan.setdefault('response_mode', 'answer')
    finish = plan.setdefault('finalization_mode', 'synthesize')
    if mode not in {'direct', 'answer', 'itinerary'} or finish not in {'forward', 'synthesize'}:
        raise ValueError('invalid response mode')
    if finish == 'forward' and mode != 'answer':
        raise ValueError('only information answers can be forwarded')
    rows = plan.setdefault('agent_schedule', [])
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
