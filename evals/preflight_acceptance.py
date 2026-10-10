"""Executable test evidence for the confirmed P01–P33 scenarios."""
import xml.etree.ElementTree as ET

SCENARIOS={
    'P01':['test_default_week_later_is_not_confirmed_and_does_not_drift'],
    'P02':['test_missing_origin_still_answers_and_queries_independent_later_leg'],
    'P03':['test_arrival_search_finds_previous_day_departure_and_preserves_evidence'],
    'P04':['test_ambiguous_day_has_explicit_nearest_future_proposal_and_keeps_original'],
    'P05':['test_default_date_can_try_next_two_days_without_changing_user_date'],
    'P06':['test_unavailable_provider_is_not_repeated_for_other_dates'],
    'P07':['test_duration_adapter_calculates_cross_year_and_more_than_24_hours','test_inconsistent_duration_does_not_create_false_arrival_date'],
    'P08':['test_price_only_request_queries_train_without_a_travel_workflow'],
    'P09':['test_default_week_later_is_not_confirmed_and_does_not_drift'],
    'P10':['test_preference_runs_once_before_first_workflow_query'],
    'P11':['test_resume_second_task_preserves_first_and_reuses_unchanged_places'],
    'P12':['test_resume_second_task_preserves_first_and_reuses_unchanged_places'],
    'P13':['test_arrival_requirement_is_preserved_without_default_departure','test_global_fixed_start_date_cannot_be_silently_changed'],
    'P14':['test_arrival_search_obeys_external_request_budget','test_tool_budget_is_cumulative_across_tasks'],
    'P15':['test_hotel_place_cannot_verify_hard_budget'],
    'P16':['test_route_skeleton_allows_unknown_origin_but_preserves_user_purpose'],
    'P17':['test_three_and_five_tasks_have_drafts_then_global_validation'],
    'P18':['test_return_with_explicit_hotel_requirement_is_allowed','test_explicit_return_hotel_is_queried_and_validated'],
    'P19':['test_three_and_five_tasks_have_drafts_then_global_validation'],
    'P20':['test_real_runtime_measures_actual_main_and_info_calls'],
    'P21':['test_second_leg_uses_reliable_previous_boundary_instead_of_week_default','test_downstream_derived_date_rechecks_new_boundary_but_preserves_user_date'],
    'P22':['test_model_cannot_finish_without_check_or_spin_forever','test_selection_requires_matching_task_query_revision_and_id'],
    'P23':['test_regenerate_task_preserves_requirements_and_only_rechecks_downstream'],
    'P24':['test_only_hotel_replacement_preserves_train_and_other_tasks','test_completed_trip_hotel_replacement_new_session_preserves_train'],
    'P25':['test_arrival_supplement_releases_only_proposed_departure_date','test_party_update_reuses_place_facts_not_old_train_pricing'],
    'P26':['test_ambiguous_feedback_saves_checkpoint_without_replanning'],
    'P27':['test_explain_saved_trip_does_not_replan_or_query'],
    'P28':['test_new_trip_has_new_identity_and_preserves_previous_trip'],
    'P29':['test_missing_origin_still_answers_and_queries_independent_later_leg'],
    'P30':['test_partial_and_completed_are_known_across_sessions_and_summary_repairs'],
    'P31':['test_completed_trip_hotel_replacement_new_session_preserves_train'],
    'P32':['test_replanning_same_trip_does_not_duplicate_history_or_statistics','test_legacy_projection_is_idempotent_and_preserves_json'],
    'P33':['test_projection_failure_does_not_fail_canonical_save','test_partial_and_completed_are_known_across_sessions_and_summary_repairs'],
}


def evidence(xml_path):
    tests=ET.parse(xml_path).getroot().findall('.//testcase')
    result={}
    for scenario,names in SCENARIOS.items():
        nodes=[node for node in tests if node.attrib['name'].split('[',1)[0] in names]
        present={node.attrib['name'].split('[',1)[0] for node in nodes}
        result[scenario]=dict(passed=present==set(names) and all(node.find('failure') is None and node.find('error') is None and node.find('skipped') is None for node in nodes),
                              tests=[node.attrib['name'] for node in nodes])
    return result
