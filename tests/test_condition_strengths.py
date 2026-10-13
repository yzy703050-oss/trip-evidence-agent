from copy import deepcopy

import pytest

from agents.workflow_contracts import create_workflow, apply_user_update
from agents.context_projection import project_model_context
from workflow_support import proposal


def soft_proposal():
    value = proposal()
    value['condition_strengths'] = {'start_date': 'soft'}
    value['tasks'][0]['condition_strengths'] = {'departure_date': 'soft'}
    return value


def test_user_soft_date_survives_creation_projection_and_copy():
    value = soft_proposal()
    original = deepcopy(value)
    w = create_workflow(value, {})
    view = project_model_context({'workflow': w, 'current_task': w['tasks'][0]})
    assert view['workflow']['condition_strengths'] == {'start_date': 'soft'}
    assert view['workflow']['tasks'][0]['condition_strengths']['departure_date'] == 'soft'
    assert view['current_task']['condition_strengths']['departure_date'] == 'soft'
    assert w['tasks'][0]['field_sources']['departure_date'] == 'user'
    assert value == original


@pytest.mark.parametrize('bad', [{'departure_date': 'optional'}, {'departure_date': True},
                                 {'constraints': 'soft'}, {'return_date': 'soft'}, []])
def test_invalid_strength_is_rejected(bad):
    value = soft_proposal()
    value['tasks'][0]['condition_strengths'] = bad
    with pytest.raises(ValueError, match='strength'):
        create_workflow(value, {})


def test_global_hard_start_cannot_be_softened_by_task():
    value = proposal()
    value['tasks'][0]['condition_strengths'] = {'departure_date': 'soft'}
    with pytest.raises(ValueError, match='hard'):
        create_workflow(value, {})


def test_legacy_source_defaults_and_inherited_hard_boundary():
    from agents.condition_strengths import condition_is_hard

    w = create_workflow(proposal(), {})
    t = w['tasks'][0]
    assert condition_is_hard(w, t, 'departure_date')
    t['field_sources']['departure_date'] = 'derived'
    assert condition_is_hard(w, t, 'departure_date')
    w['confirmed_conditions'].pop('start_date')
    assert not condition_is_hard(w, t, 'departure_date')


def test_user_update_replaces_soft_date_with_explicit_hard_date():
    w = create_workflow(soft_proposal(), {})
    updated = apply_user_update(w, {'task_updates': [
        {'task_id': w['tasks'][0]['id'], 'conditions': {'departure_date': '2026-10-12'}}
    ]})
    assert updated['tasks'][0]['condition_strengths']['departure_date'] == 'hard'
