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


GENERIC_PLACES = {
    "suggestion:city_walk": "市内漫步（地点待核实）",
    "suggestion:museum": "博物馆参观（地点待核实）",
    "suggestion:meal": "用餐休息（地点待核实）",
    "suggestion:rest": "休息（地点待核实）",
}
PLANNER_ERRORS = {
    "timeout": "行程规划失败：模型响应超时，请稍后重试。",
    "invalid_response": "行程规划失败：返回格式无效，请重试。",
    "model_error": "行程规划失败：模型服务异常，请稍后重试。",
}


def guard_itinerary(plan: dict, results: list[dict]) -> dict:
    """Only IDs in successful provider results can introduce factual output."""
    plan = plan if isinstance(plan, dict) else {}
    offers = {"train": {}, "hotel": {}}
    facts = []
    passengers = None
    guide_destination = None
    for row in results:
        if not isinstance(row, dict):
            continue
        name = row.get("agent_name")
        data = result_data(row)
        if name not in DOMAIN_AGENTS or data.get("status") not in {"ok", "partial"}:
            continue
        if name == "travel_guide":
            destination = data.get("query", {}).get("destination")
            if isinstance(destination, str) and destination:
                guide_destination = destination
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
    places = dict(GENERIC_PLACES)
    for index, fact in enumerate(facts):
        if fact["kind"] in {"place", "poi", "attraction", "location"} and fact["source"] is not None:
            places[f"guide:{index}"] = fact["content"]
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
        if raw.get("city_ref") == "guide_destination" and guide_destination:
            day["city_ref"] = "guide_destination"
            day["city"] = guide_destination
        slots = raw.get("activities", raw.get("time_slots", []))
        day["activities"] = []
        for slot in slots if isinstance(slots, list) else []:
            if not isinstance(slot, dict):
                continue
            reference = slot.get("location_ref")
            if not isinstance(reference, str) or reference not in places:
                continue
            activity = {"location_ref": reference, "location": places[reference]}
            time = slot.get("time", "")
            if isinstance(time, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?:[-–](?:[01]\d|2[0-3]):[0-5]\d)?", time):
                activity["time"] = time
            day["activities"].append(activity)
        days.append(day)
    budget = build_budget(selected["train"] if passengers else None, selected["hotel"], passengers or 1,
                          {"train", "hotel", "other"}).to_dict()
    guarded = {
        "itinerary": {"title": "行程安排建议", "duration": f"{len(days)}天", "daily_plans": days},
        "planning_complete": plan.get("planning_complete") is True,
        "selected_train_id": selected["train"].id if selected["train"] else None,
        "selected_hotel_id": selected["hotel"].id if selected["hotel"] else None,
        "selected_train": selected["train"].to_dict() if selected["train"] else None,
        "selected_hotel": selected["hotel"].to_dict() if selected["hotel"] else None,
        "guide_facts": facts,
        "budget": budget,
    }
    if "error" in plan or plan.get("status") == "error":
        code = plan.get("error_code")
        code = code if isinstance(code, str) and code in PLANNER_ERRORS else "model_error"
        guarded.update(status="error", error_code=code, error=PLANNER_ERRORS[code], planning_complete=False)
    return guarded
