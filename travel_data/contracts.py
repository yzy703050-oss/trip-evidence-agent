"""Validated domain data; unknown values remain explicit and prices stay decimal."""

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


def _json_value(value):
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class Serializable:
    def to_dict(self) -> dict:
        return _json_value(self)


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('fetched_at must be a timezone-aware datetime')


def _positive_count(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError('person count must be a positive integer')


def _stay(check_in: date, check_out: date) -> None:
    if check_out <= check_in:
        raise ValueError('check_out must be later than check_in')


def _price(value: Decimal | None, source) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal):
        raise TypeError('prices must be Decimal values from a provider')
    if not value.is_finite() or value < 0:
        raise ValueError('price must be finite and nonnegative')
    if not isinstance(source, Source):
        raise ValueError('priced offers require a source')


@dataclass(frozen=True)
class Source(Serializable):
    provider: str
    fetched_at: datetime
    url: str | None

    def __post_init__(self):
        _aware(self.fetched_at)
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError('source provider must be named')


@dataclass(frozen=True)
class AgentDataResult(Serializable):
    status: Literal['ok', 'partial', 'needs_input', 'unavailable', 'error']
    query: dict
    items: list[dict]
    missing_fields: list[str]
    source: Source | None
    fetched_at: datetime | None
    message: str | None

    def __post_init__(self):
        if self.status not in {'ok', 'partial', 'needs_input', 'unavailable', 'error'}:
            raise ValueError('invalid result status')
        if self.fetched_at is not None:
            _aware(self.fetched_at)


@dataclass(frozen=True)
class TrainQuery(Serializable):
    origin: str
    destination: str
    departure_date: date
    passengers: int = 1

    def __post_init__(self):
        _positive_count(self.passengers)


@dataclass(frozen=True)
class HotelQuery(Serializable):
    city: str
    check_in: date
    check_out: date
    guests: int

    def __post_init__(self):
        _stay(self.check_in, self.check_out)
        _positive_count(self.guests)


@dataclass(frozen=True)
class GuideQuery(Serializable):
    destination: str
    visit_dates: list[date]


@dataclass(frozen=True)
class TrainOffer(Serializable):
    id: str
    train_number: str
    origin_station: str
    destination_station: str
    departure_time: str
    arrival_time: str
    seat_class: str
    price_cny: Decimal | None
    availability: str
    remaining: int | None
    source: Source | None
    url: str | None

    def __post_init__(self):
        _price(self.price_cny, self.source)
        if self.availability == 'available' and self.price_cny is None:
            raise ValueError('available offer requires a sourced price')
        if self.remaining is not None and (type(self.remaining) is not int or self.remaining < 0):
            raise ValueError('remaining must be a nonnegative integer or None')
        if self.remaining is not None and not isinstance(self.source, Source):
            raise ValueError('inventory counts require a source')


@dataclass(frozen=True)
class HotelOffer(Serializable):
    id: str
    hotel_name: str
    room_type: str
    check_in: date
    check_out: date
    guests: int
    stay_total_cny: Decimal | None
    fees_included: bool | None
    availability: str
    cancellation: str | None
    source: Source | None
    url: str | None

    def __post_init__(self):
        _stay(self.check_in, self.check_out)
        _positive_count(self.guests)
        _price(self.stay_total_cny, self.source)
        if self.availability == 'available' and (
            self.stay_total_cny is None or self.fees_included is not True
        ):
            raise ValueError('available hotel requires a sourced total including fees')


@dataclass(frozen=True)
class GuideFact(Serializable):
    kind: str
    content: str
    verification: Literal['verified', 'needs_check']
    source: Source | None

    def __post_init__(self):
        if self.verification not in {'verified', 'needs_check'}:
            raise ValueError('invalid verification status')
        if self.verification == 'verified' and not isinstance(self.source, Source):
            raise ValueError('verified fact requires a source')
