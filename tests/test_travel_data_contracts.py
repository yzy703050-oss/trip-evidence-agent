import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from travel_data.contracts import AgentDataResult, GuideFact, HotelOffer, HotelQuery, Source, TrainOffer


def source():
    return Source('authorized-test', datetime(2026, 10, 8, tzinfo=timezone.utc), None)


def train(**changes):
    values = dict(id='t1', train_number='G1', origin_station='Shanghai',
                  destination_station='Beijing', departure_time='08:00', arrival_time='12:00',
                  seat_class='second', price_cny=Decimal('500'), availability='available',
                  remaining=None, source=source(), url=None)
    return TrainOffer(**(values | changes))


def hotel(**changes):
    values = dict(id='h1', hotel_name='Hotel', room_type='Twin', check_in=date(2026, 10, 9),
                  check_out=date(2026, 10, 11), guests=2, stay_total_cny=Decimal('800'),
                  fees_included=True, availability='available', cancellation=None,
                  source=source(), url=None)
    return HotelOffer(**(values | changes))


def test_source_rejects_naive_time():
    with pytest.raises(ValueError):
        Source('test', datetime(2026, 10, 8), None)


@pytest.mark.parametrize('factory,field', [(train, 'price_cny'), (hotel, 'stay_total_cny')])
@pytest.mark.parametrize('price', [Decimal('-1'), Decimal('NaN'), Decimal('Infinity'), '500', 500.0])
def test_offer_rejects_invalid_or_untyped_price(factory, field, price):
    with pytest.raises((ValueError, TypeError)):
        factory(**{field: price})


@pytest.mark.parametrize('factory', [train, hotel])
def test_price_requires_source(factory):
    with pytest.raises(ValueError):
        factory(source=None)


def test_hotel_dates_must_define_positive_stay():
    with pytest.raises(ValueError):
        HotelQuery('Beijing', date(2026, 10, 9), date(2026, 10, 9), 2)
    with pytest.raises(ValueError):
        hotel(check_out=date(2026, 10, 8))


def test_verified_fact_requires_source():
    with pytest.raises(ValueError):
        GuideFact('opening_hours', '09:00', 'verified', None)


def test_result_serialization_retains_null_and_decimal_strings():
    result = AgentDataResult('partial', {'date': date(2026, 10, 9)},
                             [train().to_dict()], [], source(), source().fetched_at, None)
    data = json.loads(json.dumps(result.to_dict()))
    assert data['items'][0]['price_cny'] == '500'
    assert data['items'][0]['remaining'] is None
    assert data['source']['url'] is None
    assert data['message'] is None
    assert data['query']['date'] == '2026-10-09'
    assert data['fetched_at'].endswith('+00:00')


def test_unknown_offer_keeps_missing_price_and_source():
    data = train(price_cny=None, source=None, availability='unknown').to_dict()
    assert data['price_cny'] is None
    assert data['source'] is None


def test_hotel_incomplete_fees_cannot_claim_available():
    with pytest.raises(ValueError):
        hotel(fees_included=None)
