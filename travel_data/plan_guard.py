"""Rebuild user-visible facts from provider results; discard model prose."""
from datetime import date, datetime
from decimal import Decimal
import re

from .budget import build_budget
from .contracts import Source, TrainOffer, HotelOffer, GuideFact

DOMAIN_AGENTS = {"train_search", "hotel_search", "travel_guide"}


def result_data(row):
    value = row.get("result", row).get("data", {})
    return value if isinstance(value, dict) else {}


def _source(value):
    if not isinstance(value, dict):
        return None
    return Source(value["provider"], datetime.fromisoformat(value["fetched_at"]), value.get("url"))


def _offer(item, kind):
    value = dict(item)
    value["source"] = _source(value.get("source"))
    if value["source"] is None:
        return None
    price_key = "price_cny" if kind == "train" else "stay_total_cny"
    if value.get(price_key) is not None:
        value[price_key] = Decimal(str(value[price_key]))
    if kind == "hotel":
        for key in ("check_in", "check_out"):
            value[key] = date.fromisoformat(value[key])
    return (TrainOffer if kind == "train" else HotelOffer)(**value)


def _suggestion(value):
    # Suggestion fields cannot become an alternate carrier for factual claims.
    if not isinstance(value, str) or len(value) > 100:
        return None
    if re.search(r"[0-9０-９]|元|¥|￥|票价|房价|开放|营业|预约|可订|余票|无票|无房|天气|晴|雨|雪|温度|免费|含税|取消", value):
        return None
    return value


def guard_itinerary(plan: dict, results: list[dict]) -> dict:
    """Only IDs in successful provider results can introduce factual output."""
    plan = plan if isinstance(plan, dict) else {}
    offers = {"train": {}, "hotel": {}}
    facts = []
    passengers = None
    for row in results:
        if not isinstance(row, dict):
            continue
        name = row.get("agent_name")
        data = result_data(row)
        if name not in DOMAIN_AGENTS or data.get("status") not in {"ok", "partial"}:
            continue
        if name == "train_search":
            count = data.get("query", {}).get("passengers")
            if type(count) is int and count > 0:
                passengers = count
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            try:
                if name == "travel_guide":
                    fact = GuideFact(item["kind"], item["content"], item["verification"], _source(item.get("source")))
                    if fact.source is not None or fact.verification == "needs_check":
                        facts.append(fact.to_dict())
                else:
                    kind = "train" if name == "train_search" else "hotel"
                    offer = _offer(item, kind)
                    if offer is not None:
                        offers[kind][offer.id] = offer
            except (KeyError, TypeError, ValueError, ArithmeticError):
                continue
    selected = {}
    for kind in offers:
        offer_id = plan.get(f"selected_{kind}_id")
        selected[kind] = offers[kind].get(offer_id) if isinstance(offer_id, str) else None
    original = plan.get("itinerary", {})
    original = original if isinstance(original, dict) else {}
    days = []
    raw_days = original.get("daily_plans", [])
    for raw in raw_days if isinstance(raw_days, list) else []:
        if not isinstance(raw, dict):
            continue
        day = {}
        if type(raw.get("day")) is int and 1 <= raw["day"] <= 366:
            day["day"] = raw["day"]
        try:
            day["date"] = date.fromisoformat(raw["date"]).isoformat()
        except (ValueError, TypeError, KeyError):
            pass
        city = _suggestion(raw.get("city"))
        if city:
            day["city"] = city
        slots = raw.get("activities", raw.get("time_slots", []))
        day["activities"] = []
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            location = _suggestion(slot.get("location"))
            if not location:
                continue
            activity = {"location": location}
            time = slot.get("time", "")
            if isinstance(time, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?:[-–](?:[01]\d|2[0-3]):[0-5]\d)?", time):
                activity["time"] = time
            day["activities"].append(activity)
        days.append(day)
    budget = build_budget(selected["train"] if passengers else None, selected["hotel"], passengers or 1,
                          {"train", "hotel", "other"}).to_dict()
    return {
        "itinerary": {"title": "行程安排建议", "duration": f"{len(days)}天", "daily_plans": days},
        "planning_complete": plan.get("planning_complete") is True,
        "selected_train_id": selected["train"].id if selected["train"] else None,
        "selected_hotel_id": selected["hotel"].id if selected["hotel"] else None,
        "selected_train": selected["train"].to_dict() if selected["train"] else None,
        "selected_hotel": selected["hotel"].to_dict() if selected["hotel"] else None,
        "guide_facts": facts,
        "budget": budget,
    }
