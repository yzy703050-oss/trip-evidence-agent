"""Rebuild user-visible facts from provider results; discard model prose."""

from datetime import date, datetime
from decimal import Decimal

from .contracts import HotelOffer, Source, TrainOffer
from .hotel_places import HotelPlace


def _source(value):
    if not isinstance(value, dict):
        return None
    return Source(value["provider"], datetime.fromisoformat(value["fetched_at"]), value.get("url"))


def _offer(item, kind):
    value = {k: v for k, v in item.items() if k not in {"source_id", "truncated_fields"}}
    value["source"] = _source(value.get("source"))
    if value["source"] is None:
        return None
    if kind == "hotel" and value.get("kind") == "hotel_place":
        return HotelPlace.from_dict(value, value["source"])
    price_key = "price_cny" if kind == "train" else "stay_total_cny"
    if value.get(price_key) is not None:
        value[price_key] = Decimal(str(value[price_key]))
    if kind == "hotel":
        for key in ("check_in", "check_out"):
            value[key] = date.fromisoformat(value[key])
    return (TrainOffer if kind == "train" else HotelOffer)(**value)


