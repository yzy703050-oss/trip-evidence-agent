"""Compact model-visible schemas without weakening their validation constraints."""

import json
from collections import Counter
from copy import deepcopy

# These constraints apply only to non-null instance types; combinators and literal
# constraints must keep their original anyOf branch to preserve null acceptance.
NULL_SAFE_KEYS = {
    "type",
    "description",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "properties",
    "required",
    "additionalProperties",
    "minProperties",
    "maxProperties",
}


def compact_schema(schema, *, omit_descriptions=False):
    counts = Counter()

    def clean(value, names=False):
        if isinstance(value, list):
            return [clean(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {
            k: deepcopy(v)
            if k in {"const", "enum", "examples"}
            else clean(v, k in {"properties", "$defs"})
            for k, v in value.items()
            if names
            or k
            not in (
                {"title", "default", "description", "discriminator"}
                if omit_descriptions
                else {"title", "default"}
            )
        }
        branches = result.get("anyOf", [])
        if (
            not names
            and set(result) <= {"anyOf", "description"}
            and isinstance(branches, list)
            and len(branches) == 2
            and {"type": "null"} in branches
        ):
            typed = next(b for b in branches if b != {"type": "null"})
            if isinstance(typed.get("type"), str) and set(typed) <= NULL_SAFE_KEYS:
                result = {**result, **typed, "type": [typed["type"], "null"]}
                result.pop("anyOf")
        if not names and result.get("type") == "object":
            counts[json.dumps(result, sort_keys=True, ensure_ascii=False)] += 1
        return result

    cleaned = clean(deepcopy(schema))
    shared = {}
    names = set(cleaned.get("$defs", {}))
    for key, count in counts.items():
        if count > 1 and len(key) > 160:
            index = len(shared)
            while "Shared" + str(index) in names:
                index += 1
            name = "Shared" + str(index)
            shared[key] = name
            names.add(name)

    def replace(value):
        if isinstance(value, list):
            return [replace(v) for v in value]
        if not isinstance(value, dict):
            return value
        key = json.dumps(value, sort_keys=True, ensure_ascii=False)
        if key in shared:
            return {"$ref": "#/$defs/" + shared[key]}
        return {
            k: deepcopy(v) if k in {"const", "enum", "examples"} else replace(v)
            for k, v in value.items()
        }

    result = replace(cleaned)
    if shared:
        result.setdefault("$defs", {}).update(
            {name: json.loads(key) for key, name in shared.items()}
        )
    return result
