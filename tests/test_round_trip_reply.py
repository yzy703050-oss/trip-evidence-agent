from copy import deepcopy

import pytest

from trip_assistant.trips.conditions import create_workflow


def proposal():
    return {
        "confirmed_conditions": {},
        "tasks": [
            dict(
                origin=None,
                destination="上海",
                requires_hotel=True,
                conditions={"departure_date": "2026-10-12", "nights": 3},
                field_sources={"departure_date": "user", "nights": "proposal"},
            )
        ],
    }


def context(query="我明天去上海玩"):
    return dict(
        original_query=query,
        default_round_trip=True,
        effective_preferences={"home_location": "重庆"},
    )


def test_new_trip_defaults_return_home_without_filling_stay_length():
    p = proposal()
    before = deepcopy(p)
    w = create_workflow(p, context(), require_stay_proposals=True)
    assert [(t["origin"], t["destination"]) for t in w["tasks"]] == [
        ("重庆", "上海"),
        ("上海", "重庆"),
    ]
    assert w["tasks"][0]["conditions"]["nights"] == 3
    assert w["tasks"][-1]["purpose"] == "return"
    assert not w["tasks"][-1]["requires_hotel"]
    assert "nights" not in w["tasks"][-1]["conditions"]
    assert p == before


@pytest.mark.parametrize(
    "query", ["只安排单程去上海", "去上海，不用安排回程", "去上海，暂时不回重庆"]
)
def test_explicit_one_way_is_respected(query):
    w = create_workflow(proposal(), context(query), require_stay_proposals=True)
    assert len(w["tasks"]) == 1


def test_multiple_destinations_return_once_to_home_not_temporary_origin():
    p = proposal()
    p["tasks"][0]["origin"] = "北京"
    p["tasks"].append(
        dict(
            origin="上海",
            destination="杭州",
            requires_hotel=True,
            conditions={"nights": 1},
            field_sources={"nights": "proposal"},
        )
    )
    w = create_workflow(p, context("从北京去上海，然后杭州"), require_stay_proposals=True)
    assert [(t["origin"], t["destination"]) for t in w["tasks"]] == [
        ("北京", "上海"),
        ("上海", "杭州"),
        ("杭州", "重庆"),
    ]
    closed = deepcopy(p)
    closed["tasks"].append(
        dict(origin="杭州", destination="重庆", purpose="return", requires_hotel=False)
    )
    assert len(create_workflow(closed, context(), require_stay_proposals=True)["tasks"]) == 3


def test_explicit_return_destination_is_not_overridden_by_home():
    p = proposal()
    p["tasks"].append(
        dict(
            origin="上海",
            destination="北京",
            purpose="return",
            purpose_source="user",
            requires_hotel=False,
        )
    )
    w = create_workflow(p, context("去上海玩，最后回北京"), require_stay_proposals=True)
    assert len(w["tasks"]) == 2 and w["tasks"][-1]["destination"] == "北京"


def test_declining_same_day_return_is_not_declining_return():
    w = create_workflow(
        proposal(), context("去上海住三晚，不要当天返程"), require_stay_proposals=True
    )
    assert len(w["tasks"]) == 2
