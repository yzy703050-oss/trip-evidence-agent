"""Deterministic subtotal of usable sourced offers, with explicit missing categories."""

from dataclasses import dataclass
from decimal import Decimal

from .contracts import HotelOffer, Serializable, TrainOffer, _positive_count


@dataclass(frozen=True)
class BudgetBreakdown(Serializable):
    known_subtotal_cny: Decimal
    lines: list[dict]
    missing_categories: list[str]
    complete: bool


def build_budget(
    train: TrainOffer | None,
    hotel: HotelOffer | None,
    passengers: int,
    required: set[str],
) -> BudgetBreakdown:
    _positive_count(passengers)
    if train is not None and not isinstance(train, TrainOffer):
        raise TypeError('train must be a TrainOffer')
    if hotel is not None and not isinstance(hotel, HotelOffer):
        raise TypeError('hotel must be a HotelOffer')
    lines = []
    if train is not None and train.availability == 'available' and train.price_cny is not None:
        lines.append(dict(category='train', offer_id=train.id, quantity=passengers,
                          unit_price_cny=train.price_cny,
                          amount_cny=train.price_cny * passengers, source=train.source.to_dict()))
    if (hotel is not None and hotel.availability == 'available'
            and hotel.stay_total_cny is not None and hotel.fees_included is True):
        lines.append(dict(category='hotel', offer_id=hotel.id, quantity=1,
                          amount_cny=hotel.stay_total_cny, source=hotel.source.to_dict()))
    missing = sorted(required - {line['category'] for line in lines})
    subtotal = sum((line['amount_cny'] for line in lines), Decimal('0'))
    return BudgetBreakdown(subtotal, lines, missing, not missing)
