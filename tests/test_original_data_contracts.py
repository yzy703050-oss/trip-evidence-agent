"""Offline regression scenarios grouped by responsibility."""

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from support.travel_data_contracts import hotel, source, train

from trip_assistant.providers.budget import build_budget
from trip_assistant.providers.contracts import AgentDataResult, GuideFact, HotelQuery, Source

# travel_data_contracts


def test_source_rejects_naive_time():
    with pytest.raises(ValueError):
        Source("test", datetime(2026, 10, 8), None)


@pytest.mark.parametrize("factory,field", [(train, "price_cny"), (hotel, "stay_total_cny")])
@pytest.mark.parametrize(
    "price", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), "500", 500.0]
)
def test_offer_rejects_invalid_or_untyped_price(factory, field, price):
    with pytest.raises((ValueError, TypeError)):
        factory(**{field: price})


@pytest.mark.parametrize("factory", [train, hotel])
def test_price_requires_source(factory):
    with pytest.raises(ValueError):
        factory(source=None)


@pytest.mark.parametrize("remaining", [0, 3])
def test_inventory_count_requires_source_even_without_price(remaining):
    with pytest.raises(ValueError):
        train(price_cny=None, availability="unknown", remaining=remaining, source=None)


@pytest.mark.parametrize("remaining", [0, 3])
def test_sourced_inventory_count_retains_provenance_without_price(remaining):
    data = train(price_cny=None, availability="unknown", remaining=remaining).to_dict()
    assert data["remaining"] == remaining
    assert data["source"]["provider"] == "authorized-test"
    assert data["source"]["fetched_at"].endswith("+00:00")


def test_hotel_dates_must_define_positive_stay():
    with pytest.raises(ValueError):
        HotelQuery("Beijing", date(2026, 10, 9), date(2026, 10, 9), 2)
    with pytest.raises(ValueError):
        hotel(check_out=date(2026, 10, 8))


def test_verified_fact_requires_source():
    with pytest.raises(ValueError):
        GuideFact("opening_hours", "09:00", "verified", None)


def test_result_serialization_retains_null_and_decimal_strings():
    result = AgentDataResult(
        "partial",
        {"date": date(2026, 10, 9)},
        [train().to_dict()],
        [],
        source(),
        source().fetched_at,
        None,
    )
    data = json.loads(json.dumps(result.to_dict()))
    assert data["items"][0]["price_cny"] == "500"
    assert data["items"][0]["remaining"] is None
    assert data["source"]["url"] is None
    assert data["message"] is None
    assert data["query"]["date"] == "2026-10-09"
    assert data["fetched_at"].endswith("+00:00")


def test_unknown_offer_keeps_missing_price_and_source():
    data = train(price_cny=None, source=None, availability="unknown").to_dict()
    assert data["price_cny"] is None
    assert data["source"] is None


def test_hotel_incomplete_fees_cannot_claim_available():
    with pytest.raises(ValueError):
        hotel(fees_included=None)


# travel_budget


def test_budget_multiplies_ticket_and_adds_entire_hotel_stay():
    result = build_budget(train(), hotel(), 2, {"train", "hotel"})
    assert result.known_subtotal_cny == Decimal("1800")
    assert result.complete is True
    assert result.missing_categories == []
    assert [line["amount_cny"] for line in result.lines] == [Decimal("1000"), Decimal("800")]
    assert result.to_dict()["known_subtotal_cny"] == "1800"


def test_unknown_hotel_fees_excluded_from_known_subtotal():
    result = build_budget(
        train(), hotel(fees_included=None, availability="unknown"), 2, {"train", "hotel"}
    )
    assert result.known_subtotal_cny == Decimal("1000")
    assert result.complete is False
    assert result.missing_categories == ["hotel"]


def test_unavailable_and_unknown_prices_not_counted():
    result = build_budget(
        train(availability="unavailable"),
        hotel(stay_total_cny=None, availability="unknown"),
        2,
        {"train", "hotel", "activities"},
    )
    assert result.known_subtotal_cny == Decimal("0")
    assert result.missing_categories == ["activities", "hotel", "train"]


def test_missing_optional_categories_do_not_make_budget_incomplete():
    assert build_budget(train(), None, 1, {"train"}).complete is True


def test_budget_rejects_model_text_and_invalid_passengers():
    with pytest.raises(TypeError):
        build_budget("ticket 500", None, 1, {"train"})
    with pytest.raises(ValueError):
        build_budget(train(), None, 0, {"train"})
