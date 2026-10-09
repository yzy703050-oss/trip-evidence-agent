from decimal import Decimal

import pytest

from test_travel_data_contracts import hotel, train
from travel_data.budget import build_budget


def test_budget_multiplies_ticket_and_adds_entire_hotel_stay():
    result = build_budget(train(), hotel(), 2, {'train', 'hotel'})
    assert result.known_subtotal_cny == Decimal('1800')
    assert result.complete is True
    assert result.missing_categories == []
    assert [line['amount_cny'] for line in result.lines] == [Decimal('1000'), Decimal('800')]
    assert result.to_dict()['known_subtotal_cny'] == '1800'


def test_unknown_hotel_fees_excluded_from_known_subtotal():
    result = build_budget(train(), hotel(fees_included=None, availability='unknown'),
                          2, {'train', 'hotel'})
    assert result.known_subtotal_cny == Decimal('1000')
    assert result.complete is False
    assert result.missing_categories == ['hotel']


def test_unavailable_and_unknown_prices_not_counted():
    result = build_budget(train(availability='unavailable'),
                          hotel(stay_total_cny=None, availability='unknown'), 2,
                          {'train', 'hotel', 'activities'})
    assert result.known_subtotal_cny == Decimal('0')
    assert result.missing_categories == ['activities', 'hotel', 'train']


def test_missing_optional_categories_do_not_make_budget_incomplete():
    assert build_budget(train(), None, 1, {'train'}).complete is True


def test_budget_rejects_model_text_and_invalid_passengers():
    with pytest.raises(TypeError):
        build_budget('ticket 500', None, 1, {'train'})
    with pytest.raises(ValueError):
        build_budget(train(), None, 0, {'train'})
