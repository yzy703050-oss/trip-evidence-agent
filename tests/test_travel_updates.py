from copy import deepcopy

import pytest

from workflow_support import populated_workflow


@pytest.mark.parametrize('old,changes,model_kind,expected', [
    ({'origin': None}, {'origin': '重庆'}, 'change', 'supplement'),
    ({'origin': '上海'}, {'origin': '重庆'}, 'supplement', 'change'),
    ({'conditions': {'hotel_keywords': None}}, {'hotel_keywords': '全季'}, 'change', 'supplement'),
    ({'conditions': {'nights': 2}, 'field_sources': {'nights': 'proposal'}}, {'nights': 3}, 'supplement', 'change'),
    ({'conditions': {'nights': 2}}, {'nights': 2}, 'change', 'supplement'),
    ({'conditions': {'constraints': {'seat_class': '二等座'}}},
     {'constraints': {'hotel_max_nightly_cny': 500}}, 'change', 'supplement'),
    ({'conditions': {'constraints': {'seat_class': '二等座'}}},
     {'constraints': {'seat_class': '一等座', 'hotel_max_nightly_cny': 500}}, 'supplement', 'change'),
    ({'conditions': {'departure_date': '2026-10-11'}},
     {'departure_date': '2026-10-12', 'constraints': {'hotel_max_nightly_cny': 500}}, 'supplement', 'change'),
])
def test_condition_update_type_comes_from_saved_values(old, changes, model_kind, expected):
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    task = w['tasks'][0]
    for key, value in old.items():
        if isinstance(value, dict):
            task[key].update(value)
        else:
            task[key] = value
    update = dict(update_type=model_kind, target=dict(workflow_id=w['id'], task_ids=[task['id']],
                 components=['schedule']), condition_updates=changes)
    original, original_update = deepcopy(w), deepcopy(update)
    result = apply_travel_update(w, update)
    assert result['last_update']['update_type'] == expected
    assert result['last_update']['condition_updates'] == changes
    assert w == original and update == original_update
    assert result['id'] == w['id'] and result['tasks'][0]['id'] == task['id']


@pytest.mark.parametrize('condition', [
    {'passengers': 1}, {'constraints': {'seat_class': '二等座'}},
])
def test_changes_compare_inherited_confirmed_conditions(condition):
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    w['confirmed_conditions'].update(condition)
    changes = {'passengers': 2} if 'passengers' in condition else {'constraints': {'seat_class': '一等座'}}
    result = apply_travel_update(w, dict(update_type='supplement',
        target=dict(workflow_id=w['id'], task_ids=[w['tasks'][0]['id']]), condition_updates=changes))
    assert result['last_update']['update_type'] == 'change'


def test_condition_change_in_any_selected_task_is_a_change():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    w['tasks'][1]['conditions']['nights'] = 2
    result = apply_travel_update(w, dict(update_type='supplement',
        target=dict(workflow_id=w['id'], task_ids=[t['id'] for t in w['tasks'][:2]]),
        condition_updates={'nights': 3}))
    assert result['last_update']['update_type'] == 'change'
    assert all(t['conditions']['nights'] == 3 for t in result['tasks'][:2])


def test_only_hotel_replacement_preserves_train_and_other_tasks():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    old = deepcopy(w)
    t = w['tasks'][0]
    result = apply_travel_update(w, {'update_type': 'replace',
        'target': {'workflow_id': w['id'], 'task_ids': [t['id']], 'components': ['hotel']},
        'condition_updates': {'hotel_brands': ['全季']},
        'rejected_candidate_ids': [t['draft_plan']['hotel_selection']['candidate_id']]})
    changed = result['tasks'][0]
    assert result['tasks'][1:] == old['tasks'][1:]
    assert changed['revision'] == 2
    assert changed['draft_plan']['train_selection'] == old['tasks'][0]['draft_plan']['train_selection']
    assert changed['draft_plan']['hotel_selection'] is None
    assert changed['conditions']['hotel_keywords'] == '全季'
    assert changed['update_scope'] == ['hotel']
    assert result['results_by_query'][changed['draft_plan']['train_selection']['query_id']]['task_revision'] == 2
    assert changed['rejected_candidates']['hotel'] == ['place0']
    assert w == old


def test_regenerate_task_preserves_requirements_and_only_rechecks_downstream():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    t = w['tasks'][1]
    result = apply_travel_update(w, {'update_type': 'regenerate',
        'target': {'workflow_id': w['id'], 'task_ids': [t['id']], 'components': ['train', 'hotel']}})
    assert result['tasks'][0] == w['tasks'][0]
    assert result['tasks'][1]['conditions'] == t['conditions']
    assert result['tasks'][1]['revision'] == 2
    assert result['tasks'][2]['revision'] == 2
    assert result['tasks'][2]['needs_dependency_check'] is True


def test_explicit_origin_supplement_resumes_same_task():
    from agents.travel_updates import apply_travel_update
    from test_planning_conditions import skeleton
    w = skeleton(None)
    t = w['tasks'][0]
    result = apply_travel_update(w, {'update_type': 'supplement',
        'target': {'workflow_id': w['id'], 'task_ids': [t['id']], 'components': ['train']},
        'condition_updates': {'origin': '重庆'}})
    assert result['id'] == w['id']
    assert result['tasks'][0]['id'] == t['id']
    assert result['tasks'][0]['origin'] == '重庆'


def test_origin_only_update_preserves_existing_hotel():
    from agents.travel_updates import apply_travel_update
    w=populated_workflow(); t=w['tasks'][0]
    newer=apply_travel_update(w,dict(update_type='change',target=dict(workflow_id=w['id'],task_ids=[t['id']],components=['train']),condition_updates={'origin':'重庆'}))
    assert newer['tasks'][0]['update_scope']==['train']
    assert newer['tasks'][0]['draft_plan']['hotel_selection']==t['draft_plan']['hotel_selection']


def test_wrong_workflow_or_task_cannot_be_modified():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    for target in [{'workflow_id': 'other', 'task_ids': [w['tasks'][0]['id']], 'components': ['hotel']},
                   {'workflow_id': w['id'], 'task_ids': ['missing'], 'components': ['hotel']}]:
        with pytest.raises(ValueError):
            apply_travel_update(w, {'target': target, 'update_type': 'replace'})


def test_route_insertion_preserves_unchanged_leg_identities():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    rows = [dict(origin=a, destination=b, requires_hotel=b != '上海', purpose='return' if b == '上海' else 'visit')
            for a, b in zip(['上海', '北京', '苏州', '杭州'], ['北京', '苏州', '杭州', '上海'])]
    result = apply_travel_update(w, {'update_type': 'change_route',
        'target': {'workflow_id': w['id'], 'task_ids': [], 'components': ['route']}, 'route_tasks': rows})
    assert result['tasks'][0]['id'] == w['tasks'][0]['id']
    assert result['tasks'][-1]['id'] == w['tasks'][-1]['id']
    assert result['tasks'][1]['id'] not in {t['id'] for t in w['tasks']}
    assert result['tasks'][2]['depends_on'] == [result['tasks'][1]['id']]


def test_completed_trip_can_be_reopened_without_resetting_preferences():
    from agents.travel_updates import apply_travel_update
    w = populated_workflow()
    w['status'] = 'completed'
    result = apply_travel_update(w, {'update_type': 'replace',
        'target': {'workflow_id': w['id'], 'task_ids': [w['tasks'][0]['id']], 'components': ['train']}})
    assert result['status'] == 'running'
    assert result['confirmed_conditions'] == w['confirmed_conditions']
    assert result['validation'] is None
