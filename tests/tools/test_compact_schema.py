import json
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from trip_assistant.tools.registry import build_registry


def test_native_schema_is_compact_without_mutating_runtime_contract():
    tool = build_registry({})["write_trip"]
    original = deepcopy(tool.arguments.model_json_schema())
    schema = tool.schema
    # The bound reply spec gets at most 1500 extra bytes; legacy validators stay compatible.
    assert len(json.dumps(schema, ensure_ascii=False).encode()) < 12000
    assert tool.arguments.model_json_schema() == original
    Draft202012Validator.check_schema(schema["function"]["parameters"])


def test_compaction_preserves_business_property_names_and_literal_objects():
    from trip_assistant.tools.schema import compact_schema

    schema = {
        "type": "object",
        "title": "Metadata",
        "properties": {
            "title": {"type": "string", "default": "x"},
            "default": {"const": {"title": "business", "default": "value"}},
        },
    }
    compact = compact_schema(schema)
    assert compact["properties"]["title"] == {"type": "string"}
    assert compact["properties"]["default"]["const"] == {"title": "business", "default": "value"}
    value = {"title": "name", "default": {"title": "business", "default": "value"}}
    assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(
        value
    )


@pytest.mark.parametrize(
    "typed", [{"type": "string", "maxLength": 3}, {"type": "number", "minimum": 0}]
)
def test_nullable_schema_is_shorter_with_identical_validation(typed):
    from trip_assistant.tools.schema import compact_schema

    schema = {"type": "object", "properties": {"value": {"anyOf": [typed, {"type": "null"}]}}}
    compact = compact_schema(schema)
    assert len(json.dumps(compact)) < len(json.dumps(schema))
    for value in (None, "abc", "long text", -1, 0, 3, True, {}, []):
        assert Draft202012Validator(schema).is_valid({"value": value}) == Draft202012Validator(
            compact
        ).is_valid({"value": value})


@pytest.mark.parametrize("case", ["valid", "negative_money", "unknown_field", "invalid_source"])
def test_native_schema_keeps_validation_constraints(case):
    tool = build_registry({})["write_trip"]
    value = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "conditions": {"departure_date": "2026-10-17"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
    }
    task = value["tripplan"]["tasks"][0]
    if case == "negative_money":
        task["conditions"]["constraints"] = {"train_max_total_cny": -1}
    elif case == "unknown_field":
        task["status"] = "completed"
    elif case == "invalid_source":
        task["content"] = [
            {"purpose": "游览", "description": "散步", "source": "fabricated", "strength": "soft"}
        ]
    validator = Draft202012Validator(tool.schema["function"]["parameters"])
    assert validator.is_valid(value) is (case == "valid")
    if case == "valid":
        assert tool.validate(value)["tripplan"]["tasks"]
    else:
        with pytest.raises(ValueError):
            tool.validate(value)


def test_nullable_compaction_keeps_outer_intersection_constraints():
    from trip_assistant.tools.schema import compact_schema

    schema = {"type": "integer", "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]}
    compact = compact_schema(schema)
    for value in (None, 0, 1.5, -1):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(
            compact
        ).is_valid(value)


@pytest.mark.parametrize(
    "constraint", [{"enum": ["train"]}, {"const": "train"}, {"allOf": [{"type": "string"}]}]
)
def test_nullable_compaction_keeps_constraints_that_would_reject_null(constraint):
    from trip_assistant.tools.schema import compact_schema

    schema = {"anyOf": [{"type": "string", **constraint}, {"type": "null"}]}
    compact = compact_schema(schema)
    for value in (None, "train", "hotel", 1):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(
            compact
        ).is_valid(value)


def test_shared_definitions_never_overwrite_existing_contract_names():
    from trip_assistant.tools.schema import compact_schema

    repeated = {"type": "object", "properties": {f"field{i}": {"type": "string"} for i in range(8)}}
    schema = {
        "type": "object",
        "$defs": {"Shared0": {"type": "integer"}},
        "properties": {
            "first": repeated,
            "second": deepcopy(repeated),
            "code": {"$ref": "#/$defs/Shared0"},
        },
    }
    compact = compact_schema(schema)
    assert compact["$defs"]["Shared0"] == {"type": "integer"}
    assert Draft202012Validator(compact).is_valid({"first": {}, "second": {}, "code": 3})
