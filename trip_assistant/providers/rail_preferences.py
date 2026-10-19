"""Evidence-based rail classification and soft ranking before bounded paging."""

import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
KINDS = ("high_speed", "emu", "other")
PREFERENCE_KEYS = {"preferred_train_kind", "departure_after", "departure_before", "arrival_before"}
CLOCK = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def train_kind(number):
    if not isinstance(number, str):
        return "unknown"
    number = number.strip().upper()
    if not re.fullmatch(r"(?:[GDCZTKLSY]\d+|\d+)(?:/(?:[GDCZTKLSY]?\d+))?", number):
        return "unknown"
    return "high_speed" if number.startswith("G") else "emu" if number.startswith("D") else "other"


def validate_rail_preferences(value):
    if not isinstance(value, dict) or set(value) - PREFERENCE_KEYS:
        raise ValueError("unsupported rail preferences")
    for key, val in value.items():
        if key == "preferred_train_kind":
            if val not in ("high_speed", "emu", "any"):
                raise ValueError("invalid preferred train kind")
        elif not isinstance(val, str) or not CLOCK.fullmatch(val):
            raise ValueError("invalid rail preference time")
    return deepcopy(value)


def _stamp(item, name):
    try:
        value = datetime.fromisoformat(item.get(name + "_at"))
        return value.astimezone(BEIJING) if value.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _clock(item, name, stamp):
    if stamp is not None:
        return stamp.strftime("%H:%M")
    value = item.get(name + "_time")
    return value if isinstance(value, str) and CLOCK.fullmatch(value) else None


def departure_time_matches(item, constraints):
    lower = constraints.get("departure_time_after")
    upper = constraints.get("departure_time_before")
    if lower is None and upper is None:
        return True
    clock = _clock(item, "departure", _stamp(item, "departure"))
    if clock is None:
        return False
    return (lower is None or clock >= lower) and (upper is None or clock <= upper)
