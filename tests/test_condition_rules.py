"""Offline regression scenarios grouped by responsibility."""

import pytest
from support.workflow import proposal
from support.workflow_queries import train_result

from trip_assistant.trips.conditions import create_workflow
from trip_assistant.trips.evidence import record_query

# condition_strengths


def soft_proposal():
    value = proposal()
    value["condition_strengths"] = {"start_date": "soft"}
    value["tasks"][0]["condition_strengths"] = {"departure_date": "soft"}
    return value


@pytest.mark.parametrize(
    "bad",
    [
        {"departure_date": "optional"},
        {"departure_date": True},
        {"constraints": "soft"},
        {"return_date": "soft"},
        [],
    ],
)
def test_invalid_strength_is_rejected(bad):
    value = soft_proposal()
    value["tasks"][0]["condition_strengths"] = bad
    with pytest.raises(ValueError, match="strength"):
        create_workflow(value, {})


def test_global_hard_start_cannot_be_softened_by_task():
    value = proposal()
    value["tasks"][0]["condition_strengths"] = {"departure_date": "soft"}
    with pytest.raises(ValueError, match="hard"):
        create_workflow(value, {})


def test_legacy_source_defaults_and_inherited_hard_boundary():
    from trip_assistant.trips.strengths import condition_is_hard

    w = create_workflow(proposal(), {})
    t = w["tasks"][0]
    assert condition_is_hard(w, t, "departure_date")
    t["field_sources"]["departure_date"] = "derived"
    assert condition_is_hard(w, t, "departure_date")
    w["confirmed_conditions"].pop("start_date")
    assert not condition_is_hard(w, t, "departure_date")


# condition_conflicts


def dated_workflow(*, soft=False):
    return create_workflow(
        {
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "requires_hotel": False,
                    "conditions": {
                        "departure_date": "2026-10-20",
                        "arrival_before": "2026-10-20T10:00:00+08:00",
                    },
                    "field_sources": {"departure_date": "user", "arrival_before": "user"},
                    "condition_strengths": {
                        "departure_date": "soft" if soft else "hard",
                        "arrival_before": "hard",
                    },
                }
            ]
        },
        {},
    )


def add_trains(w):
    task = w["tasks"][0]
    data = train_result(task)
    return record_query(w, task["id"], data["query"], {}, data, {}, domain="train")


def test_known_late_candidates_produce_sourced_hard_conflict():
    from trip_assistant.trips.conflicts import find_hard_date_conflict

    w = dated_workflow()
    row = add_trains(w)
    conflict = find_hard_date_conflict(w)
    assert conflict["code"] == "hard_date_conflict"
    assert conflict["affected_task_ids"] == [w["tasks"][0]["id"]]
    assert row["id"] in conflict["evidence_refs"]
    assert "10:00" in conflict["reason"] and "question" not in conflict


@pytest.mark.parametrize("case", ["alternative", "unknown", "failed", "stale", "soft", "empty"])
def test_missing_evidence_or_possible_alternative_does_not_force_question(case):
    from trip_assistant.trips.conflicts import find_hard_date_conflict

    w = dated_workflow(soft=case == "soft")
    row = add_trains(w)
    if case == "alternative":
        row["items"][-1]["arrival_at"] = "2026-10-20T09:00:00+08:00"
        row["items"][-1]["arrival_time"] = "09:00"
    elif case == "unknown":
        row["items"][-1]["arrival_at"] = None
    elif case == "failed":
        row["needs_revalidation"] = True
    elif case == "stale":
        w["tasks"][0]["revision"] += 1
    elif case == "empty":
        row["items"] = []
    assert find_hard_date_conflict(w) is None


def test_contradictory_hard_dates_need_no_supplier_evidence():
    from trip_assistant.trips.conflicts import find_hard_date_conflict

    w = dated_workflow()
    w["tasks"][0]["conditions"]["departure_date"] = "2026-10-21"
    assert find_hard_date_conflict(w)["code"] == "hard_date_conflict"
