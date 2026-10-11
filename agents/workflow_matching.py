"""Small trip identities for model matching, without importing session history."""
from copy import deepcopy


def workflow_identity(workflow):
    fields = ('id', 'revision', 'status', 'original_query', 'created_at', 'updated_at',
              'confirmed_conditions', 'field_sources')
    value = {key: deepcopy(workflow.get(key)) for key in fields}
    value['tasks'] = [
        {key: deepcopy(task.get(key)) for key in
         ('id', 'origin', 'destination', 'requires_hotel', 'conditions', 'field_sources')}
        for task in workflow['tasks']
    ]
    return value


def validate_match(value, candidates):
    if not isinstance(value, dict):
        raise ValueError('invalid workflow match')
    action = value.get('action')
    allowed = {'new': {'action'}, 'reuse': {'action', 'workflow_id', 'refresh'},
               'clarify': {'action', 'question'}}
    if action not in allowed or set(value) - allowed[action]:
        raise ValueError('invalid workflow match action')
    if action == 'reuse':
        if value.get('workflow_id') not in {w['id'] for w in candidates}:
            raise ValueError('workflow match target was not provided')
        if 'refresh' in value and type(value['refresh']) is not bool:
            raise ValueError('invalid workflow refresh')
    if action == 'clarify' and (not isinstance(value.get('question'), str) or not value['question'].strip()):
        raise ValueError('workflow match needs a question')
    return deepcopy(value)
