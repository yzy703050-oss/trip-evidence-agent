"""Offline regression scenarios grouped by responsibility."""

import pytest
from support.workflow import proposal, workflow

from trip_assistant.trips.conditions import create_workflow, effective_conditions

# workflow_contracts


def test_defaults_are_not_confirmed_and_three_tasks_have_stable_dependencies():
    w = workflow()
    assert w["effective_conditions"]["passengers"] == 1
    assert w["effective_conditions"]["guests"] == 1
    assert w["field_sources"]["passengers"] == "default"
    assert w["field_sources"]["guests"] == "derived"
    assert w["effective_conditions"]["constraints"] == {}
    assert w["confirmed_conditions"].get("passengers") is None
    assert [t["status"] for t in w["tasks"]] == ["pending"] * 3
    assert w["tasks"][-1]["requires_hotel"] is False
    assert w["tasks"][1]["depends_on"] == [w["tasks"][0]["id"]]


def test_conditions_merge_without_overwriting_input_or_forcing_brand():
    confirmed = {"passengers": 2, "constraints": {"seat_class": "二等座"}}
    value, sources = effective_conditions(confirmed, {"passengers": 3}, {"hotel_brand": "如家"})
    assert value["passengers"] == value["guests"] == 3
    assert value["constraints"] == {"seat_class": "二等座"}
    assert sources["passengers"] == "user"
    assert confirmed["passengers"] == 2


@pytest.mark.parametrize("count", [0, -1, True, "1"])
def test_invalid_counts_are_rejected(count):
    p = proposal()
    p["confirmed_conditions"]["passengers"] = count
    with pytest.raises(ValueError):
        create_workflow(p, {})


def test_broken_route_and_invalid_dependency_are_rejected():
    for mutate in [
        lambda p: p["tasks"][1].update(origin="苏州"),
        lambda p: p["tasks"][0].update(depends_on=[1]),
        lambda p: p["tasks"][0].update(status="validated"),
    ]:
        p = proposal()
        mutate(p)
        with pytest.raises(ValueError):
            create_workflow(p, {})


def test_unknown_dates_do_not_silently_become_today():
    p = proposal()
    p["confirmed_conditions"] = {}
    for t in p["tasks"]:
        t["conditions"] = {}
        t["field_sources"] = {}
    w = create_workflow(p, {"current_time": "2026-10-10T10:00:00+08:00"})
    assert w["confirmed_conditions"].get("start_date") is None
    assert w["tasks"][0]["conditions"].get("departure_date") is None


def test_first_departure_can_inherit_explicit_start_date():
    p = proposal()
    p["tasks"][0]["conditions"].pop("departure_date")
    w = create_workflow(p, {})
    assert w["tasks"][0]["conditions"]["departure_date"] == "2026-10-11"
    assert w["tasks"][0]["field_sources"]["departure_date"] == "derived"


def test_redundant_route_and_scope_metadata_is_checked_then_canonicalized():
    p = proposal()
    p["confirmed_conditions"].update(
        origin="上海",
        destinations=["北京", "杭州"],
        return_to="上海",
        constraints={
            "only_train_hotel": True,
            "no_weather": True,
            "no_guide": True,
            "total_budget_cny": "1000",
        },
    )
    w = create_workflow(p, {})
    assert "origin" not in w["confirmed_conditions"]
    assert w["confirmed_conditions"]["constraints"] == {"total_budget_cny": "1000"}
    p["confirmed_conditions"]["return_to"] = "苏州"
    with pytest.raises(ValueError):
        create_workflow(p, {})


# intent_contracts


def direct(**changes):
    return dict(response_mode="direct", final_answer="你好", agent_schedule=[], **changes)
