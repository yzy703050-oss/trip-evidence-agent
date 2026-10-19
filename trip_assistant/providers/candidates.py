"""One-turn request deduplication and bounded candidate views."""

from copy import deepcopy
from decimal import Decimal


def candidate_view(result, *, constraints=None, ranking=None, offset=0, limit=5, preferences=None):
    from datetime import datetime

    from trip_assistant.providers.rail_preferences import departure_time_matches, train_kind

    if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
        raise ValueError("invalid candidate page")
    value = deepcopy(result.to_dict() if hasattr(result, "to_dict") else result)
    constraints = constraints or {}
    ranking = ranking or []
    all_items = value.get("items", [])
    items = []
    unknown = set()
    query = value.get("query", {})
    for item in all_items:
        gaps = []
        violates = False
        for key, expected in constraints.items():
            actual = None
            match = None
            if key in {"train_max_total_cny", "hotel_max_total_cny", "hotel_max_nightly_cny"}:
                raw = item.get("price_cny" if key.startswith("train") else "stay_total_cny")
                if raw is not None:
                    actual = Decimal(str(raw))
                    if key.startswith("train"):
                        actual *= query.get("passengers", 1)
                    if key == "hotel_max_nightly_cny":
                        try:
                            nights = (
                                datetime.fromisoformat(item["check_out"])
                                - datetime.fromisoformat(item["check_in"])
                            ).days
                            actual = actual / nights if nights > 0 else None
                        except (KeyError, ValueError, TypeError):
                            actual = None
                    if actual is not None:
                        match = actual <= Decimal(str(expected))
            elif key == "available_only":
                if not expected:
                    match = True
                elif item.get("availability") not in (None, "unknown"):
                    match = item["availability"] == "available"
            elif key == "seat_class":
                if item.get("seat_class") is not None:
                    match = item["seat_class"] == expected
            elif key == "train_kind":
                kind = train_kind(item.get("train_number"))
                if kind != "unknown":
                    match = kind == expected
            elif key.startswith("departure_time_"):
                match = departure_time_matches(item, {key: expected})
            elif key in {"arrival_before", "arrival_date"}:
                try:
                    stamp = datetime.fromisoformat(item["arrival_at"])
                    if stamp.tzinfo is not None:
                        match = (
                            stamp <= datetime.fromisoformat(expected)
                            if key == "arrival_before"
                            else stamp.date().isoformat() == expected
                        )
                except (KeyError, TypeError, ValueError):
                    pass
            elif key == "hotel_keywords":
                match = any(word in item.get("hotel_name", "") for word in expected.split())
            elif key in {"total_budget_cny"}:
                continue
            if match is False:
                violates = True
                break
            if match is None:
                gaps.append(key)
        if not violates:
            if gaps:
                item["unverified_constraints"] = gaps
                unknown.update(gaps)
            items.append(item)

    def sort_value(item, rule):
        field = rule["field"]
        v = item.get(field)
        if field == "train_kind":
            v = train_kind(item.get("train_number"))
            v = None if v == "unknown" else v
        if field == "duration_minutes":
            try:
                v = (
                    datetime.fromisoformat(item["arrival_at"])
                    - datetime.fromisoformat(item["departure_at"])
                ).total_seconds() / 60
            except (KeyError, ValueError, TypeError):
                v = None
        if field == "nightly_cny":
            try:
                v = (
                    Decimal(str(item["stay_total_cny"]))
                    / (
                        datetime.fromisoformat(item["check_out"])
                        - datetime.fromisoformat(item["check_in"])
                    ).days
                )
            except (KeyError, ValueError, TypeError, ArithmeticError):
                v = None
        if rule.get("preferred") is not None:
            preferred = rule["preferred"]
            preferred = preferred if isinstance(preferred, list) else [preferred]
            return (
                0 if v in preferred or isinstance(v, str) and any(p in v for p in preferred) else 1
            )
        if v is None:
            return None
        if field.endswith("_cny") or field in {"duration_minutes", "distance_m"}:
            return Decimal(str(v))
        if field.endswith("_at"):
            try:
                return datetime.fromisoformat(v).timestamp()
            except (ValueError, TypeError):
                return None
        return v

    for rule in reversed(ranking):
        known = [i for i in items if sort_value(i, rule) is not None]
        missing = [i for i in items if sort_value(i, rule) is None]
        known.sort(key=lambda i: sort_value(i, rule), reverse=rule.get("direction") == "desc")
        items = known + missing
    # Unverified hard requirements never outrank known eligible evidence.
    items.sort(key=lambda i: bool(i.get("unverified_constraints")))
    value.update(
        candidate_total=len(items),
        excluded_candidate_count=len(all_items) - len(items),
        items=items[offset : offset + limit],
        view_offset=offset,
        has_more=offset + limit < len(items),
    )
    if unknown:
        value["unverified_constraints"] = sorted(unknown)
        if value.get("status") == "ok":
            value["status"] = "partial"
    return value
