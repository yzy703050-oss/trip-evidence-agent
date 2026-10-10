from copy import deepcopy
import pytest
from agents.workflow_guard import check_task, check_workflow, finalize_workflow
from agents.workflow_contracts import apply_user_update
from workflow_support import populated_workflow


def test_places_can_complete_recommendations_but_not_verify_prices():
    w = populated_workflow()
    checked = check_workflow(w)
    assert checked['valid'] is True
    assert checked['budget']['verified'] is False
    assert checked['budget']['known_subtotal_cny'] == '300'
    assert all(t['status'] == 'draft' for t in w['tasks'])
    final = finalize_workflow(w, checked)
    assert final['status'] == 'completed'
    assert all(t['status'] == 'validated' for t in final['tasks'])
    assert 'activities' not in str(final['tasks'])


def test_hotel_place_cannot_verify_hard_budget():
    w = populated_workflow(); w['confirmed_conditions']['constraints'] = {'total_budget_cny': '1000'}
    checked = check_workflow(w)
    assert checked['valid'] is False and checked['budget']['verified'] is False
    assert 'hotel_price_unknown' in {i['code'] for i in checked['issues']}


def test_decimal_budget_includes_every_train_and_hotel():
    w = populated_workflow(quoted=True)
    w['confirmed_conditions']['constraints'] = {'total_budget_cny': '699.99'}
    checked = check_workflow(w)
    assert checked['budget']['known_subtotal_cny'] == '700'
    assert checked['budget']['verified'] is True
    assert 'total_budget_exceeded' in {i['code'] for i in checked['issues']}


def test_unknown_arrival_day_cannot_be_invented_in_draft():
    w = populated_workflow(); t = w['tasks'][0]
    q = w['results_by_query'][t['draft_plan']['train_selection']['query_id']]
    q['items'][0]['arrival_at'] = None
    t['draft_plan']['schedule']['arrival_at'] = '2026-10-11T12:00:00+08:00'
    checked = check_task(w, t['id'], t['draft_plan'])
    assert checked['valid'] is False
    assert checked['reconstructed_plan']['schedule']['arrival_at'] is None
    assert 'arrival_time_unknown' in {i['code'] for i in checked['issues']}


def test_verified_cross_day_cannot_check_in_before_arrival():
    w = populated_workflow(); t = w['tasks'][0]
    q = w['results_by_query'][t['draft_plan']['train_selection']['query_id']]
    q['items'][0].update(departure_time='23:00', arrival_time='12:00', departure_at='2026-10-11T23:00:00+08:00',
                         arrival_at='2026-10-12T12:00:00+08:00')
    checked = check_task(w, t['id'], t['draft_plan'])
    assert 'hotel_before_arrival' in {i['code'] for i in checked['issues']}
    assert checked['valid'] is False


def test_later_departure_cannot_precede_previous_stay():
    w = populated_workflow(); t = w['tasks'][1]
    q = w['results_by_query'][t['draft_plan']['train_selection']['query_id']]
    t['conditions']['departure_date'] = '2026-10-12'
    q['parameters']['departure_date'] = '2026-10-12'
    q['items'][0]['departure_at'] = '2026-10-12T08:00:00+08:00'
    q['items'][0]['arrival_at'] = '2026-10-12T12:00:00+08:00'
    checked = check_workflow(w)
    assert 'departure_before_previous_checkout' in {i['code'] for i in checked['issues']}


def test_revision_change_invalidates_global_check():
    w = populated_workflow(); checked = check_workflow(w)
    revised = apply_user_update(w, {'confirmed_conditions': {'passengers': 3}})
    with pytest.raises(ValueError): finalize_workflow(revised, checked)
    assert check_workflow(revised)['valid'] is False


def test_wrong_domain_forged_candidate_and_activities_are_rejected():
    w = populated_workflow(); t = w['tasks'][0]
    for draft in [dict(t['draft_plan'], activities=[]),
                  dict(t['draft_plan'], train_selection=t['draft_plan']['hotel_selection']),
                  dict(t['draft_plan'], train_selection=dict(t['draft_plan']['train_selection'], candidate_id='fake'))]:
        assert check_task(w, t['id'], draft)['valid'] is False


def test_refresh_failure_is_not_current_validated_evidence():
    w = populated_workflow(); next(iter(w['results_by_query'].values()))['needs_revalidation'] = True
    assert check_workflow(w)['valid'] is False
