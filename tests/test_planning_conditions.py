"""Offline regression scenarios grouped by responsibility."""

from copy import deepcopy

import pytest
from support.planning_conditions import skeleton

from trip_assistant.trips.conditions import create_workflow

# planning_conditions


def test_route_skeleton_allows_unknown_origin_but_preserves_user_purpose():
    w = skeleton(None)
    assert w["tasks"][0]["origin"] is None
    assert w["tasks"][0]["purpose"] == "visit"
    assert w["tasks"][0]["status"] == "pending"


def test_consistent_model_route_metadata_does_not_require_another_model_call():
    p = dict(
        confirmed_conditions={"destination": "上海", "requires_hotel": True, "scope": "交通与住宿"},
        tasks=[
            dict(
                origin=None,
                destination="上海",
                requires_hotel=True,
                conditions={"departure_date": None, "passengers": 1},
                field_sources={
                    "origin": "missing",
                    "departure_date": "missing",
                    "passengers": "default",
                },
            )
        ],
    )
    w = create_workflow(p, {})
    assert w["confirmed_conditions"] == {}
    assert w["tasks"][0]["field_sources"]["passengers"] == "default"
    p["confirmed_conditions"]["destination"] = "北京"
    with pytest.raises(ValueError):
        create_workflow(p, {})


def test_unknown_purpose_and_prefixed_sources_are_projected_without_changing_dates():
    p = dict(
        confirmed_conditions={},
        tasks=[
            dict(
                origin="重庆",
                destination="上海",
                requires_hotel=True,
                purpose="unspecified",
                purpose_source="unspecified",
                conditions={"arrival_date": "2026-10-18"},
                field_sources={"conditions.arrival_date": "user"},
            )
        ],
    )
    w = create_workflow(p, {})
    assert w["tasks"][0]["purpose_source"] == "proposal"
    assert w["tasks"][0]["field_sources"]["arrival_date"] == "user"


def test_return_with_explicit_hotel_requirement_is_allowed():
    p = {
        "confirmed_conditions": {},
        "tasks": [
            dict(origin="上海", destination="北京", purpose="visit", requires_hotel=True),
            dict(
                origin="北京",
                destination="上海",
                purpose="return",
                purpose_source="user",
                requires_hotel=True,
            ),
        ],
    }
    w = create_workflow(p, {})
    assert w["tasks"][-1]["purpose"] == "return"
    assert w["tasks"][-1]["requires_hotel"] is True


def test_invalid_known_route_still_rejected():
    p = {
        "confirmed_conditions": {},
        "tasks": [
            dict(origin="上海", destination="北京", requires_hotel=False),
            dict(origin="杭州", destination="上海", requires_hotel=False),
        ],
    }
    with pytest.raises(ValueError):
        create_workflow(deepcopy(p), {})


# home_origin_default


def route(origin=None, destination="上海"):
    return dict(
        confirmed_conditions={},
        tasks=[
            dict(
                origin=origin,
                destination=destination,
                requires_hotel=True,
                conditions={"nights": 2},
            )
        ],
    )


def test_home_is_default_first_origin_with_preference_evidence():
    proposal = route()
    original = deepcopy(proposal)
    workflow = create_workflow(proposal, {"effective_preferences": {"home_location": "重庆"}})
    assert workflow["tasks"][0]["origin"] == "重庆"
    assert workflow["tasks"][0]["field_sources"]["origin"] == "preference"
    assert proposal == original


def test_explicit_origin_and_subsequent_routes_take_priority():
    proposal = route("北京")
    proposal["tasks"].append(dict(origin=None, destination="杭州", requires_hotel=False))
    workflow = create_workflow(proposal, {"effective_preferences": {"home_location": "重庆"}})
    assert [task["origin"] for task in workflow["tasks"]] == ["北京", "上海"]


@pytest.mark.parametrize("home", [None, "", "上海"])
def test_unknown_or_same_city_home_does_not_create_invalid_train_route(home):
    workflow = create_workflow(route(), {"effective_preferences": {"home_location": home}})
    assert workflow["tasks"][0]["origin"] is None
