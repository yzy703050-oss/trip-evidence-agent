import pytest
from support.workflow import populated_workflow

from trip_assistant.runtime.scheduler import Scheduler
from trip_assistant.settings import Settings
from trip_assistant.storage.trips import TripStore


@pytest.fixture
def service(tmp_path):
    from trip_assistant.trips.service import TripService

    return TripService(TripStore(str(tmp_path), "a"), None, Scheduler(Settings()))


@pytest.mark.parametrize("key,value", [("seat_class", "二等座"), ("train_max_total_cny", 50)])
def test_item_cannot_soften_inherited_hard_filter(service, key, value):
    work = populated_workflow(quoted=True)
    work["confirmed_conditions"]["constraints"] = {key: value}
    with pytest.raises(ValueError, match="hard"):
        service._effective_item(
            work,
            work["tasks"][0],
            {"domain": "train", "parameters": {}, "condition_strengths": {key: "soft"}},
        )


def test_policy_filter_cannot_be_softened_by_item(service):
    work = populated_workflow(quoted=True)
    work["policy_context"] = {
        "constraints": [
            {"field": "seat_class", "value": "二等座", "hard": True, "applicability": {}}
        ]
    }
    value = service._effective_item(
        work,
        work["tasks"][0],
        {"domain": "train", "parameters": {}, "condition_strengths": {"seat_class": "soft"}},
    )
    assert value["constraints"]["seat_class"] == "二等座"


def test_soft_filter_is_retained_as_metadata_but_does_not_exclude(service):
    work = populated_workflow(quoted=True)
    value = service._effective_item(
        work,
        work["tasks"][0],
        {
            "domain": "train",
            "parameters": {},
            "constraints": {"train_kind": "high_speed"},
            "condition_strengths": {"train_kind": "soft"},
        },
    )
    assert (
        value["condition_strengths"]["train_kind"] == "soft"
        and "train_kind" not in value["constraints"]
    )


def test_large_evidence_pool_allows_a_real_selected_reference():
    from trip_assistant.trips.evidence import resolve_selection

    work = populated_workflow(quoted=True)
    task = work["tasks"][0]
    ref = task["draft_plan"]["train_selection"]
    row = work["results_by_query"][ref["query_id"]]
    row["items"] = [{**row["items"][0], "id": f"c{i}"} for i in range(100)]
    assert (
        resolve_selection(work, task["id"], task["revision"], {**ref, "candidate_id": "c99"})["id"]
        == "c99"
    )


def test_soft_budget_does_not_make_unpriced_hotel_a_hard_failure():
    from trip_assistant.trips.validation import check_workflow

    work = populated_workflow(quoted=False)
    work["confirmed_conditions"]["constraints"] = {"hotel_max_nightly_cny": 500}
    work["condition_strengths"]["hotel_max_nightly_cny"] = "soft"
    assert check_workflow(work)["valid"]


def test_hard_hotel_brand_is_checked_again_after_selection():
    from trip_assistant.trips.validation import check_workflow

    work = populated_workflow(quoted=True)
    task = work["tasks"][0]
    task["conditions"]["hotel_keywords"] = "汉庭"
    task["condition_strengths"]["hotel_keywords"] = "hard"
    checked = check_workflow(work)
    assert not checked["valid"] and any(
        i["code"] == "hotel_keyword_constraint_failed" for i in checked["issues"]
    )


def test_legacy_string_budget_and_equal_model_number_are_equivalent(service):
    work = populated_workflow(quoted=True)
    work["confirmed_conditions"]["constraints"] = {"train_max_total_cny": "500"}
    item = service._effective_item(
        work,
        work["tasks"][0],
        {"domain": "train", "parameters": {}, "constraints": {"train_max_total_cny": 500}},
    )
    assert str(item["constraints"]["train_max_total_cny"]) == "500"
