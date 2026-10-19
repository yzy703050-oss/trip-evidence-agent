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


def test_settings_use_confirmed_limits_without_legacy_route_config():
    from trip_assistant.settings import Settings

    s = Settings(_env_file=None)
    assert (s.planning_rounds, s.tools_per_batch, s.external_queries_per_batch) == (8, 8, 12)
    assert (s.external_concurrency, s.supplier_concurrency, s.trip_write_concurrency) == (3, 2, 1)
    assert (s.turn_timeout, s.request_timeout, s.extra_retries) == (600, 30, 2)
    assert (
        s.failure_window,
        s.failure_threshold,
        s.cooldown,
        s.half_open_concurrency,
        s.recovery_successes,
    ) == (60, 5, 60, 1, 2)


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
