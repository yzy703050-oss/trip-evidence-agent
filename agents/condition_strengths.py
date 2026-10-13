"""Date permissions independent of provenance, with conservative legacy defaults."""

from copy import deepcopy

DATE_FIELDS = frozenset({'start_date', 'end_date', 'departure_date', 'arrival_date',
                         'arrival_before', 'check_in', 'check_out', 'nights'})


def validate_condition_strengths(value, conditions):
    if not isinstance(value, dict) or set(value) - DATE_FIELDS:
        raise ValueError('invalid condition strengths')
    if any(level not in {'hard', 'soft'} for level in value.values()):
        raise ValueError('invalid condition strength level')
    if any(conditions.get(key) is None for key in value):
        raise ValueError('condition strength requires a condition value')
    return deepcopy(value)


def global_is_hard(workflow, key):
    return (workflow.get('confirmed_conditions', {}).get(key) is not None
            and workflow.get('condition_strengths', {}).get(key, 'hard') == 'hard')


def condition_is_hard(workflow, task, key):
    if global_is_hard(workflow, key):
        return True
    if (key == 'departure_date' and workflow.get('tasks')
            and task['id'] == workflow['tasks'][0]['id']
            and global_is_hard(workflow, 'start_date')):
        return True
    if key in task.get('condition_strengths', {}):
        return task['condition_strengths'][key] == 'hard'
    if key in task.get('conditions', {}):
        return task.get('field_sources', {}).get(key) in {'user', 'context'}
    return False


def validate_inherited_strengths(workflow):
    for task in workflow['tasks']:
        for key, level in task.get('condition_strengths', {}).items():
            inherited = global_is_hard(workflow, key) or (
                key == 'departure_date' and task is workflow['tasks'][0]
                and global_is_hard(workflow, 'start_date'))
            if level == 'soft' and inherited:
                raise ValueError('task cannot soften an inherited hard condition')


def mark_user_conditions(target, conditions, strengths=None):
    """Only the user update path may replace a fixed condition or its strength."""
    levels = {key: 'hard' for key in conditions if key in DATE_FIELDS}
    if strengths is not None:
        levels.update(validate_condition_strengths(strengths, conditions))
    target.setdefault('condition_strengths', {}).update(levels)
