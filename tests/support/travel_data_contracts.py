"""Offline fixtures extracted from test_travel_data_contracts; no test-module imports."""

from datetime import UTC, date, datetime
from decimal import Decimal

from trip_assistant.providers.contracts import HotelOffer, Source, TrainOffer


def source():
    return Source("authorized-test", datetime(2026, 10, 8, tzinfo=UTC), None)


def train(**changes):
    values = dict(
        id="t1",
        train_number="G1",
        origin_station="Shanghai",
        destination_station="Beijing",
        departure_time="08:00",
        arrival_time="12:00",
        seat_class="second",
        price_cny=Decimal("500"),
        availability="available",
        remaining=None,
        source=source(),
        url=None,
    )
    return TrainOffer(**(values | changes))


def hotel(**changes):
    values = dict(
        id="h1",
        hotel_name="Hotel",
        room_type="Twin",
        check_in=date(2026, 10, 9),
        check_out=date(2026, 10, 11),
        guests=2,
        stay_total_cny=Decimal("800"),
        fees_included=True,
        availability="available",
        cancellation=None,
        source=source(),
        url=None,
    )
    return HotelOffer(**(values | changes))
