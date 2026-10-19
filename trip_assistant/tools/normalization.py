"""Only canonicalize unambiguous representations; never repair business decisions."""

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.trips.contracts import FILTER_KEYS


def normalize_tool_arguments(name, arguments, turn):
    result = deepcopy(arguments)
    if name not in {"write_trip", "query"}:
        return result
    timezone = turn.get("timezone") or turn.get("system_context", {}).get("timezone")

    def metadata(values, parent, path, name):
        safe_values = (
            {"hard", "soft"}
            if name == "condition_strengths"
            else {"user", "context", "preference", "derived", "default", "proposal"}
        )
        conditions = parent.get("conditions", parent.get("confirmed_conditions", parent))
        declared = conditions.get("constraints", {}) if isinstance(conditions, dict) else {}
        declared = declared if isinstance(declared, dict) else {}
        known = FILTER_KEYS | {"total_budget_cny"}

        def invalid(field, proposed):
            example_value = (
                proposed
                if isinstance(proposed, str) and proposed in safe_values
                else ("soft" if name == "condition_strengths" else "proposal")
            )
            # Show shape and small known enum values, never arbitrary free-form input.
            detail = {
                "field": path + "." + field,
                "type": "invalid_constraint_metadata",
                "message": "约束来源和强度须按叶字段填写，不能给constraints整体标注或使用未知键。",
                "received_type": type(proposed).__name__,
                "expected": "以train_kind等具体约束叶字段为键；仅重发受影响的最小补丁。",
                "example": {
                    key: example_value
                    for key in list(declared or {"train_kind": None})[:3]
                    if key in known
                },
            }
            if isinstance(proposed, str) and proposed in safe_values:
                detail["received"] = proposed
            raise ToolFailure(
                "INVALID_ARGUMENTS",
                "unknown constraint metadata field: " + field,
                {"fields": [detail]},
            )

        canonical = {}
        for key, value in values.items():
            if key == "constraints":
                if isinstance(value, dict):
                    entries = value.items()
                elif (
                    len(declared) == 1
                    and set(declared) <= known
                    and isinstance(value, str)
                    and value in safe_values
                ):
                    entries = [(next(iter(declared)), value)]
                else:
                    invalid(key, value)
            else:
                entries = [(key, value)]
            for field, proposed in entries:
                leaf = field.removeprefix("constraints.")
                target = leaf if leaf in known else field
                if key == "constraints" and target not in known:
                    invalid(key + "." + field, proposed)
                if target in canonical and canonical[target] != proposed:
                    raise ToolFailure(
                        "INVALID_ARGUMENTS",
                        "conflicting metadata: " + target,
                        {
                            "fields": [
                                {
                                    "field": path + "." + target,
                                    "type": "conflicting_metadata",
                                    "message": "同一叶字段存在冲突的元数据，保留一个明确值。",
                                }
                            ]
                        },
                    )
                canonical[target] = proposed
        return canonical

    def walk(value, path):
        if isinstance(value, list):
            return [walk(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        cleaned = {}
        for key, item in value.items():
            if key == "arrival_before" and isinstance(item, str):
                stamp = datetime.fromisoformat(item)
                if stamp.tzinfo is None:
                    if timezone != "Asia/Shanghai":
                        raise ToolFailure(
                            "INVALID_ARGUMENTS", "arrival_before requires explicit timezone"
                        )
                    item = stamp.replace(tzinfo=ZoneInfo(timezone)).isoformat()
            elif key in {"field_sources", "condition_strengths"} and isinstance(item, dict):
                item = metadata(item, value, path + "." + key, key)
            else:
                item = walk(item, path + "." + key)
            if (
                name == "write_trip"
                and key in {"conditions", "confirmed_conditions"}
                and isinstance(item, dict)
                and isinstance(item.get("constraints", {}), dict)
            ):
                # These are already published filter names, never inferred business rules.
                for field in item.keys() & (FILTER_KEYS | {"total_budget_cny"}):
                    proposed = item.pop(field)
                    constraints = item.setdefault("constraints", {})
                    if field in constraints and constraints[field] != proposed:
                        raise ToolFailure("INVALID_ARGUMENTS", "conflicting constraint: " + field)
                    constraints[field] = proposed
            cleaned[key] = item
        return cleaned

    # Do not interpret arbitrary policy context or free-form prose as tool parameters.
    field = "tripplan" if name == "write_trip" else "query_plan"
    if field in result:
        result[field] = walk(result[field], field)
    return result
