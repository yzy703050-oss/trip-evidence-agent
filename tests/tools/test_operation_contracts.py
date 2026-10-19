import pytest
from jsonschema import Draft202012Validator

from trip_assistant.tools.registry import build_registry


@pytest.mark.parametrize(
    "field,value", [("origin", "重庆"), ("conditions", {}), ("items", []), ("content", [])]
)
def test_selection_contract_excludes_creation_fields(field, value):
    tool = build_registry({})["write_trip"]
    args = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "select",
            "trip_plan_id": "t",
            "expected_revision": 1,
            "tasks": [{"task_id": "task", field: value}],
        },
    }
    assert not Draft202012Validator(tool.schema["function"]["parameters"]).is_valid(args)
    with pytest.raises(ValueError):
        tool.validate(args)


def test_proposal_contract_rejects_execution_identity():
    tool = build_registry({})["write_trip"]
    with pytest.raises(ValueError):
        tool.validate(
            {
                "request_kind": "planning",
                "tripplan": {
                    "operation": "propose",
                    "tasks": [{"task_id": "invented", "origin": "重庆", "destination": "上海"}],
                },
            }
        )


@pytest.mark.parametrize("patch", [{"trip_plan_id": None}, {"expected_revision": None}])
def test_existing_trip_identity_is_required_by_schema_and_validator(patch):
    tool = build_registry({})["write_trip"]
    args = {
        "request_kind": "planning",
        "tripplan": {"operation": "select", "trip_plan_id": "t", "expected_revision": 1, **patch},
    }
    assert not Draft202012Validator(tool.schema["function"]["parameters"]).is_valid(args)
    with pytest.raises(ValueError):
        tool.validate(args)
