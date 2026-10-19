"""User hard departure windows are checked before paging and on cached selection."""

from copy import deepcopy

import pytest
from support.rail_preferences import offer, view
from support.workflow import populated_workflow

from trip_assistant.trips.validation import check_task


def test_exact_departure_time_filters_before_paging():
    items = [
        offer("G1", departure="09:59"),
        offer("G2", departure="10:00"),
        offer("G3", departure="10:01"),
    ]
    constraints = {"departure_time_after": "10:00", "departure_time_before": "10:00"}
    result = view(items, constraints=constraints, limit=1)
    assert result["candidate_total"] == 1 and result["items"][0]["id"] == "G2"
    assert view(items, constraints=constraints, offset=1)["items"] == []


@pytest.mark.parametrize(
    "constraints, expected",
    [
        ({"departure_time_after": "10:00"}, ["G2", "G3"]),
        ({"departure_time_before": "10:00"}, ["G1", "G2"]),
        ({"departure_time_after": "10:00", "departure_time_before": "11:00"}, ["G2", "G3"]),
    ],
)
def test_bounds_include_user_boundary(constraints, expected):
    items = [
        offer("G1", departure="09:59"),
        offer("G2", departure="10:00"),
        offer("G3", departure="11:00"),
    ]
    assert [i["id"] for i in view(items, constraints=constraints)["items"]] == expected


@pytest.mark.parametrize("clock", ["", None, "25:00", "unknown"])
def test_unknown_or_invalid_clock_never_satisfies_upper_bound(clock):
    assert (
        view([offer("G1", departure=clock)], constraints={"departure_time_before": "10:00"})[
            "items"
        ]
        == []
    )


@pytest.mark.parametrize(
    "constraints",
    [
        {"departure_time_after": "10:00"},
        {"departure_time_before": "07:00"},
        {"departure_time_after": "10:00", "departure_time_before": "10:00"},
    ],
)
def test_current_hard_time_cannot_be_bypassed_by_unfiltered_cached_reference(constraints):
    w = populated_workflow()
    task = w["tasks"][0]  # Current selected train leaves 08:00.
    task["conditions"]["constraints"] = constraints
    before = deepcopy(w)
    checked = check_task(w, task["id"], task["draft_plan"])
    assert not checked["valid"]
    assert any(i["code"] == "departure_time_constraint_failed" for i in checked["issues"])
    assert w == before


def test_known_time_uses_beijing_timezone_and_default_soft_time_cannot_override():
    from trip_assistant.providers.rail_preferences import departure_time_matches

    utc = offer("G1", departure="00:00", departure_at="2026-10-15T00:00:00+00:00")
    assert departure_time_matches(
        utc, {"departure_time_after": "08:00", "departure_time_before": "08:00"}
    )
    early = offer("G2", departure="06:00")
    assert view(
        [offer("G1"), early],
        constraints={"departure_time_after": "06:00", "departure_time_before": "06:00"},
    )["items"] == [early]


def test_final_guard_accepts_matching_exact_hard_time():
    w = populated_workflow()
    task = w["tasks"][0]
    task["conditions"]["constraints"] = {
        "departure_time_after": "08:00",
        "departure_time_before": "08:00",
    }
    assert check_task(w, task["id"], task["draft_plan"])["valid"]
