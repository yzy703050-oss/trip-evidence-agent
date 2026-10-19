"""Bounded model/tool views, independent of the complete candidate cache."""

import hashlib
import json
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation

COMMON = {"id", "source", "url", "availability", "remaining", "kind", "verification"}
FIELDS = {
    "train": COMMON
    | {
        "train_number",
        "origin_station",
        "destination_station",
        "departure_at",
        "arrival_at",
        "departure_time",
        "arrival_time",
        "time_evidence",
        "seat_class",
        "price_cny",
    },
    "hotel": COMMON
    | {
        "hotel_name",
        "room_type",
        "address",
        "city",
        "check_in",
        "check_out",
        "guests",
        "stay_total_cny",
        "fees_included",
        "cancellation",
        "rating",
        "distance_m",
        "longitude",
        "latitude",
        "location",
        "district",
    },
    "weather": COMMON
    | {"city", "date", "description", "temp_c", "min_temp_c", "max_temp_c", "weather"},
}
META = {
    "id",
    "task_id",
    "task_revision",
    "request_id",
    "query_id",
    "result_revision",
    "status",
    "domain",
    "source",
    "source_id",
    "fetched_at",
    "missing_fields",
    "message",
    "query",
    "parameters",
    "constraints",
    "candidate_total",
    "returned_candidate_total",
    "excluded_candidate_count",
    "view_offset",
    "needs_revalidation",
    "refresh_status",
    "unverified_constraints",
}


def _project_candidate_item(domain, item, limits, requirements):
    """Copy allowed facts, derive quoted nightly cost, and bound long text."""
    allowed = FIELDS.get(domain)
    if allowed is not None and limits.get("model_view", True):
        optional = {"rating", "distance_m", "cancellation", "longitude", "latitude"}
        allowed = (allowed - optional) | (set(requirements or ()) & optional)
        allowed -= {"time_evidence", "location", "district", "url"}
    row = {k: deepcopy(v) for k, v in item.items() if allowed is None or k in allowed}
    if domain == "hotel" and limits.get("model_view", True):
        # Recompute from quote evidence; never trust a supplied derived field.
        try:
            nights = (
                date.fromisoformat(row.get("check_out")) - date.fromisoformat(row.get("check_in"))
            ).days
            total_cost = Decimal(str(row.get("stay_total_cny")))
            if nights > 0 and total_cost.is_finite() and total_cost >= 0:
                row["derived_stay_nights"] = nights
                row["derived_nightly_cny"] = format(total_cost / nights, "f")
        except (ValueError, TypeError, InvalidOperation):
            pass
    for name in ("departure", "arrival"):
        if limits.get("model_view", True) and row.get(name + "_at"):
            row.pop(name + "_time", None)
    truncated = []
    for field, default in {
        "description": 128,
        "name": 256,
        "hotel_name": 256,
        "address": 256,
    }.items():
        length = limits.get(field + "_length", default)
        if isinstance(row.get(field), str) and len(row[field]) > length:
            row[field] = row[field][:length]
            truncated.append(field)
    if truncated:
        row["truncated_fields"] = truncated
    return row


def build_tool_result_view(domain, result, *, limits, requirements=None):
    view = {k: deepcopy(v) for k, v in result.items() if k in META}
    maximum = limits.get("candidate_limit", 5)
    offset = limits.get("offset", result.get("view_offset", 0))
    if type(maximum) is not int or maximum < 0 or type(offset) is not int or offset < 0:
        raise ValueError("invalid result window")
    items, seen = [], set()
    query = result.get("query") or result.get("parameters") or {}
    for item in result.get("items", []):
        if domain == "weather":
            if query.get("date") and item.get("date") != query["date"]:
                continue
            key = (query.get("city") or item.get("city"), item.get("date"))
        else:
            key = (
                item.get("id"),
                item.get("seat_class"),
                item.get("room_type"),
                item.get("departure_at"),
                item.get("check_in"),
                item.get("check_out"),
            )
        if key in seen:
            continue
        seen.add(key)
        items.append(_project_candidate_item(domain, item, limits, requirements))
    total = result.get("candidate_total")
    total = total if type(total) is int else len(items)
    maximum = min(maximum, 1) if domain == "weather" else maximum
    start = 0 if limits.get("already_paged") else offset
    page = items[start : start + maximum]
    view.update(
        items=page,
        candidate_total=total,
        view_offset=offset,
        has_more=offset + len(page) < total,
    )
    if isinstance(view.get("message"), str) and len(view["message"]) > limits.get(
        "message_length", 256
    ):
        view["message"] = view["message"][: limits.get("message_length", 256)]
        view["truncated_fields"] = ["message"]
    sources = {}
    for obj in [view, *view["items"]]:
        source = obj.get("source")
        if isinstance(source, dict):
            identity = hashlib.sha256(
                json.dumps(source, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            sources[identity] = {
                k: v
                for k, v in source.items()
                if k in {"provider", "fetched_at", "document_id", "chunk_id"}
            }
            obj["source"] = (
                sources[identity] if limits.get("model_view", True) else deepcopy(source)
            )
            obj["source_id"] = identity
    if sources:
        view["sources"] = sources
    if limits.get("model_view", True):
        for row in view["items"]:
            parent = view.get("source") or {}
            child = row.get("source") or {}
            if row.get("source_id") and (
                row.get("source_id") == view.get("source_id")
                or child.get("provider") == parent.get("provider")
                and all(parent.get(k) == v for k, v in child.items())
            ):
                row.pop("source", None)
    return view


def remember_view(turn, view):
    if not view.get("query_id"):
        return
    key = f"{view['query_id']}:{view['result_revision']}"
    views = turn.setdefault("candidate_views", {})
    views[key] = {
        k: deepcopy(view.get(k)) for k in ("constraints", "ranking", "view_offset", "has_more")
    }
    while len(views) > 64:
        views.pop(next(iter(views)))
    for item in view.get("items", []):
        if item.get("id"):
            turn.setdefault("shown_candidates", {})[item["id"]] = {
                "query_id": view["query_id"],
                "result_revision": view["result_revision"],
            }
