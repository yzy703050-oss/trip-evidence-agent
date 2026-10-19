import pytest
from support.rail_preferences import offer, view


def test_no_hidden_default_changes_supplier_order():
    assert view([offer("K1", price="10"), offer("G1", price="200")])["items"][0]["id"] == "K1"


def test_model_explicit_arrival_ranking_changes_order():
    assert (
        view(
            [
                offer("G1", arrival_at="2026-10-20T23:30:00+08:00"),
                offer("D1", price="200", arrival_at="2026-10-20T16:00:00+08:00"),
            ],
            ranking=[{"field": "arrival_at", "direction": "asc"}],
        )["items"][0]["id"]
        == "D1"
    )


def test_full_sort_before_pagination_preserves_all_candidates():
    items = [offer(f"K{i}", price=str(i + 1)) for i in range(8)] + [offer("G1", price="200")]
    pages = [
        view(items, offset=i, limit=5, ranking=[{"field": "train_kind", "preferred": "high_speed"}])
        for i in (0, 5)
    ]
    assert pages[0]["items"][0]["id"] == "G1"
    assert pages[0]["candidate_total"] == 9
    assert len({i["id"] for p in pages for i in p["items"]}) == 9


def test_explicit_soft_preferences_override_defaults():
    prefs = {
        "rail_preferences": {
            "preferred_train_kind": "emu",
            "departure_after": "18:00",
            "departure_before": "23:00",
        }
    }
    assert (
        view(
            [offer("G1"), offer("D1", departure="19:00", arrival="20:00")],
            preferences=prefs,
            ranking=[{"field": "train_kind", "preferred": "emu"}],
        )["items"][0]["id"]
        == "D1"
    )


def test_hard_kind_filters_only_explicit_constraint():
    items = [offer("G1", departure="06:00", arrival="08:30"), offer("D1"), offer("K1")]
    assert view(items, constraints={"train_kind": "high_speed"})["items"] == [items[0]]
    assert view(items)["candidate_total"] == 3
    assert view(items[1:], constraints={"train_kind": "high_speed"})["items"] == []


@pytest.mark.parametrize(
    "number,kind",
    [
        ("G25", "high_speed"),
        ("d6", "emu"),
        ("K2", "other"),
        ("Z1", "other"),
        ("1234", "other"),
        (None, "unknown"),
        ("marketing", "unknown"),
    ],
)
def test_kind_is_grounded_in_train_number(number, kind):
    from trip_assistant.providers.rail_preferences import train_kind

    assert train_kind(number) == kind


@pytest.mark.parametrize(
    "value",
    [
        [],
        None,
        {"unknown": 1},
        {"preferred_train_kind": "plane"},
        {"departure_after": "25:00"},
        {"arrival_before": True},
    ],
)
def test_invalid_soft_preferences_rejected(value):
    from trip_assistant.providers.rail_preferences import validate_rail_preferences

    with pytest.raises(ValueError):
        validate_rail_preferences(value)


def test_seat_preference_and_hard_early_departure_take_priority():
    items = [offer("G1", price="10"), {**offer("G2", price="200"), "seat_class": "一等座"}]
    assert (
        view(items, ranking=[{"field": "seat_class", "preferred": "一等座"}])["items"][0]["id"]
        == "G2"
    )
    early = offer("G3", departure="06:00", arrival="08:30")
    assert view(items + [early], constraints={"departure_time_before": "07:00"})["items"] == [early]
