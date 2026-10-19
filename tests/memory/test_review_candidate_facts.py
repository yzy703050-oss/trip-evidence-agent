from copy import deepcopy

import pytest

from trip_assistant.memory.planning import _review_view


def candidate_view(short_departure="11:20", full_departure="2026-10-22T11:20:00+08:00"):
    return {
        "domain": "train",
        "query_id": "q",
        "result_revision": 4,
        "items": [
            {
                "origin_station": "上海",
                "destination_station": "重庆",
                "id": "candidate-11",
                "train_number": "G9005",
                "departure_time": short_departure,
                "arrival_time": "18:20",
                "seat_class": "二等座",
                "price_cny": "320",
                "departure_at": full_departure,
                "arrival_at": "2026-10-22T18:20:00+08:00",
                "time_evidence": {"arrival": "simulation"},
            }
        ],
    }


def test_review_groups_identity_and_full_facts_without_changing_audit_or_order():
    view = candidate_view()
    view["items"].append({**view["items"][0], "id": "candidate-12", "train_number": "G9006"})
    before = deepcopy(view)
    projected = _review_view(view)
    first = projected["items"][0]
    assert list(first)[:6] == [
        "id",
        "train_number",
        "departure_at",
        "arrival_at",
        "seat_class",
        "price_cny",
    ]
    assert "departure_time" not in first and "arrival_time" not in first
    assert first["time_evidence"] == before["items"][0]["time_evidence"]
    assert [x["id"] for x in projected["items"]] == ["candidate-11", "candidate-12"]
    assert projected["query_id"] == "q" and projected["result_revision"] == 4
    assert view == before


@pytest.mark.parametrize(
    "short,full",
    [
        ("09:20", "2026-10-22T11:20:00+08:00"),
        ("11:20", None),
        ("11:20", "invalid"),
    ],
)
def test_inconsistent_or_unparseable_short_time_is_not_hidden(short, full):
    row = _review_view(candidate_view(short, full))["items"][0]
    assert row["departure_time"] == short


@pytest.mark.parametrize(
    "stamp,period", [("11:20", "上午"), ("12:00", "下午"), ("18:20", "晚上"), ("03:00", "凌晨")]
)
def test_review_adds_factual_period_label_without_mutating_candidate(stamp, period):
    original = candidate_view(stamp, f"2026-10-22T{stamp}:00+08:00")
    before = deepcopy(original)
    result = _review_view(original)["items"][0]
    assert result["departure_period"] == period
    assert result["arrival_period"] == "晚上"
    assert original == before


def test_unknown_timestamp_gets_no_derived_period():
    row = _review_view(candidate_view("11:20", "invalid"))["items"][0]
    assert "departure_period" not in row
