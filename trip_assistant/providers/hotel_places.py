"""Hotel locations are reference candidates, never room offers or inventory."""

import math
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from .contracts import Serializable, Source, _positive_count, _stay


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _location(value):
    if not isinstance(value, str):
        raise ValueError("hotel location is missing")
    longitude, latitude = (float(part) for part in value.split(","))
    if not (
        math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    ):
        raise ValueError("hotel location is invalid")
    return value


def _photos(value):
    photos = []
    for row in value if isinstance(value, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("url"), str):
            continue
        try:
            parsed = urlparse(row["url"])
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                photos.append({"title": _text(row.get("title")) or "", "url": row["url"]})
        except ValueError:
            continue
    return photos


@dataclass(frozen=True)
class HotelPlaceQuery(Serializable):
    city: str
    keywords: str = "酒店"
    check_in: date | None = None
    check_out: date | None = None
    guests: int | None = None
    search_kind: str = field(default="hotel_place", init=False)

    def __post_init__(self):
        if not _text(self.city):
            raise ValueError("city is required")
        if not _text(self.keywords) or len(self.keywords) > 80:
            raise ValueError("keywords must contain 1 to 80 characters")
        for value in (self.check_in, self.check_out):
            if value is not None and type(value) is not date:
                raise ValueError("hotel date is invalid")
        if self.check_in is not None and self.check_out is not None:
            _stay(self.check_in, self.check_out)
        if self.guests is not None:
            _positive_count(self.guests)

    def to_dict(self):
        return {key: value for key, value in super().to_dict().items() if value is not None}


def make_hotel_place_query(fields):
    city = _text(fields.get("city", fields.get("destination")))
    if city is None:
        return None, ["city"]
    values = {"city": city, "keywords": fields.get("keywords", "酒店")}
    for key in ("check_in", "check_out"):
        value = fields.get(key)
        if value is not None:
            try:
                values[key] = date.fromisoformat(value) if isinstance(value, str) else value
            except ValueError:
                return None, [key]
    if fields.get("guests") is not None:
        values["guests"] = fields["guests"]
    try:
        return HotelPlaceQuery(**values), []
    except (ValueError, TypeError):
        return None, ["query"]


@dataclass(frozen=True)
class HotelPlace(Serializable):
    id: str
    hotel_name: str
    address: str
    city: str
    district: str
    location: str
    source: Source
    phone: str | None = None
    rating: str | None = None
    photos: list[dict] = field(default_factory=list)

    def __post_init__(self):
        if not _text(self.id) or not _text(self.hotel_name) or not isinstance(self.source, Source):
            raise ValueError("hotel place requires a name, id and source")
        _location(self.location)

    def to_dict(self):
        return {
            **super().to_dict(),
            "kind": "hotel_place",
            "room_type": None,
            "stay_total_cny": None,
            "fees_included": None,
            "availability": "unknown",
            "cancellation": None,
            "url": self.source.url,
        }

    @classmethod
    def from_dict(cls, value, source):
        if value.get("availability", "unknown") != "unknown" or any(
            value.get(key) is not None
            for key in ("room_type", "stay_total_cny", "fees_included", "cancellation")
        ):
            raise ValueError("hotel places cannot assert room prices or inventory")
        return cls(
            id=value["id"],
            hotel_name=value["hotel_name"],
            address=_text(value.get("address")) or "",
            city=_text(value.get("city")) or "",
            district=_text(value.get("district")) or "",
            location=value["location"],
            source=source,
            phone=_text(value.get("phone")),
            rating=_text(value.get("rating")),
            photos=_photos(value.get("photos")),
        )


def hotel_place_view(value, *, limit, offset, constraints, preferences):
    brands = preferences.get("hotel_brands") or []
    brands = [brands] if isinstance(brands, str) else brands if isinstance(brands, list) else []
    brands = [brand for brand in brands if isinstance(brand, str) and brand]
    items = sorted(
        value["items"],
        key=lambda item: not any(brand in item.get("hotel_name", "") for brand in brands),
    )
    value["candidate_total"] = len(items)
    value["items"] = items[offset : offset + limit]
    unchecked = [
        key
        for key in ("hotel_max_total_cny", "hotel_max_nightly_cny", "available_only")
        if constraints.get(key) is not None and constraints.get(key) is not False
    ]
    if unchecked:
        value["unverified_constraints"] = unchecked
        if value["status"] == "ok":
            value["status"] = "partial"
        value["message"] = (
            value.get("message") or ""
        ) + "预算和空房条件未验证，以下仅为酒店地点参考。"
    return value
