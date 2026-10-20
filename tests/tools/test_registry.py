import pytest


def test_actual_schemas_and_validation_agree():
    from trip_assistant.tools.registry import build_registry

    registry = build_registry({})
    assert set(registry) == {"read", "query", "write_memory", "write_trip", "query_policy"}
    read = registry["read"]
    assert read.schema["function"]["parameters"]["additionalProperties"] is False
    assert (
        read.validate({"request_kind": "lookup", "resource": "trip_summaries"})["resource"]
        == "trip_summaries"
    )
    with pytest.raises(ValueError):
        read.validate(
            {"request_kind": "lookup", "resource": "trip_summaries", "execute_trip": True}
        )
    assert registry["write_memory"].category == "simple_write"
    assert registry["write_trip"].category == "complex_write"


@pytest.mark.parametrize(
    "field",
    [
        "planning_rounds",
        "external_concurrency",
        "supplier_concurrency",
        "turn_timeout",
        "request_timeout",
        "failure_window",
        "failure_threshold",
        "cooldown",
        "recovery_successes",
    ],
)
def test_invalid_limits_rejected_at_configuration_boundary(field):
    from trip_assistant.settings import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: 0})
