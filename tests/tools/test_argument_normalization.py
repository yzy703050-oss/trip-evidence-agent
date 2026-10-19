from copy import deepcopy

import pytest
from pydantic import ValidationError


def normalize(args, context=None):
    from trip_assistant.tools.normalization import normalize_tool_arguments

    return normalize_tool_arguments("write_trip", args, context or {})


def test_explicit_timezone_completes_format_without_changing_audit_input():
    raw = {"tripplan": {"tasks": [{"conditions": {"arrival_before": "2026-10-21T09:00"}}]}}
    original = deepcopy(raw)
    result = normalize(raw, {"timezone": "Asia/Shanghai"})
    assert (
        result["tripplan"]["tasks"][0]["conditions"]["arrival_before"]
        == "2026-10-21T09:00:00+08:00"
    )
    assert raw == original


def test_explicit_offset_is_not_reinterpreted():
    raw = {"tripplan": {"tasks": [{"conditions": {"arrival_before": "2026-10-21T09:00+09:00"}}]}}
    assert normalize(raw, {"timezone": "Asia/Shanghai"}) == raw


def test_unknown_timezone_is_not_guessed_from_host():
    with pytest.raises(ValueError, match="timezone"):
        normalize({"tripplan": {"tasks": [{"conditions": {"arrival_before": "2026-10-21T09:00"}}]}})


def test_known_metadata_spellings_are_equivalent_and_conflicts_are_rejected():
    raw = {
        "tripplan": {
            "tasks": [
                {
                    "field_sources": {"constraints": {"seat_class": "context"}},
                    "condition_strengths": {"constraints.seat_class": "hard", "seat_class": "hard"},
                }
            ]
        }
    }
    row = normalize(raw)["tripplan"]["tasks"][0]
    assert row["field_sources"] == {"seat_class": "context"}
    assert row["condition_strengths"] == {"seat_class": "hard"}
    raw["tripplan"]["tasks"][0]["condition_strengths"]["seat_class"] = "soft"
    with pytest.raises(ValueError, match="conflict"):
        normalize(raw)


def test_null_and_clear_operations_are_not_erased():
    raw = {
        "tripplan": {
            "operation": "amend",
            "tasks": [{"content": [], "conditions": {"constraints": {"seat_class": None}}}],
        }
    }
    assert normalize(raw) == raw


def test_validation_feedback_contains_fields_without_inputs_or_documentation_urls():
    from trip_assistant.runtime.errors import error_payload
    from trip_assistant.tools.registry import ReadArguments

    try:
        ReadArguments.model_validate(
            {"request_kind": "planning", "resource": "trip", "offset": "private-long-input"}
        )
    except ValidationError as error:
        details = error_payload(error)["details"]
    assert details["fields"][0]["field"] == "offset"
    assert "private-long-input" not in str(details) and "https://" not in str(details)


async def test_runtime_normalizes_before_side_effects_and_retains_original_call(tmp_path):
    from support.native import NativeRuntimeFixture

    from evals.simulated_travel import SimulatedTrainProvider

    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    plan = {
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-20"},
                "items": [{"domain": "train"}],
            }
        ],
    }
    plan["tasks"][0]["conditions"]["arrival_before"] = "2026-10-22T09:00"
    plan["tasks"][0].setdefault("field_sources", {})["arrival_before"] = "user"
    plan["tasks"][0].setdefault("condition_strengths", {})["arrival_before"] = "hard"
    runtime.model.script = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "p",
                    "name": "write_trip",
                    "input": {"request_kind": "planning", "tripplan": plan},
                }
            ]
        },
        "已保留查询草案。",
    ]
    result = await runtime.run("22日上午9点前抵达上海", context={"timezone": "Asia/Shanghai"})
    assert result["tool_results"][0]["status"] == "partial"
    assert len(runtime.model.messages) == 2 and len(train.calls) == 1
    original = runtime.sessions.read_events(event_type="tool_call")[0]["tool_calls"][0]
    assert (
        original["arguments"]["tripplan"]["tasks"][0]["conditions"]["arrival_before"]
        == "2026-10-22T09:00"
    )
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert saved["tasks"][0]["conditions"]["arrival_before"] == "2026-10-22T09:00:00+08:00"


def test_inherited_start_date_error_identifies_the_actual_global_source(tmp_path):
    from support.native import NativeRuntimeFixture

    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = {
        "default_round_trip": False,
        "confirmed_conditions": {"start_date": "2026-10-21"},
        "condition_strengths": {"start_date": "hard"},
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-20"},
                "condition_strengths": {"departure_date": "soft"},
                "items": [{"domain": "train"}],
            }
        ],
    }
    with pytest.raises(ToolFailure) as caught:
        runtime.trip_service.preview(plan, {})
    assert caught.value.code == "CONDITION_SCOPE_CONFLICT"
    assert caught.value.details["inherited_from"] == "confirmed_conditions.start_date"
    assert caught.value.details["global_value"] == "2026-10-21"
    assert "start_date" in str(caught.value)
    assert not runtime.memory.workflow_store.list_all()


@pytest.mark.parametrize("mode", [True, False])
def test_known_filter_leaf_moves_to_constraint_without_changing_meaning(mode):
    raw = {
        "tripplan": {
            "tasks": [
                {
                    "conditions": {"departure_date": "2026-10-21", "departure_time_after": "19:00"},
                    "condition_strengths": {"departure_time_after": "soft"},
                    "field_sources": {"departure_time_after": "proposal"},
                }
            ]
        }
    }
    before = deepcopy(raw)
    row = normalize(raw, {"single_pass_planning": mode})["tripplan"]["tasks"][0]
    assert row["conditions"] == {
        "departure_date": "2026-10-21",
        "constraints": {"departure_time_after": "19:00"},
    }
    assert row["condition_strengths"] == {"departure_time_after": "soft"}
    assert raw == before
    assert normalize(raw)["tripplan"]["tasks"][0] == row


def test_single_pass_filter_relocation_rejects_conflicting_duplicate_values():
    raw = {
        "tripplan": {
            "tasks": [
                {
                    "conditions": {
                        "departure_time_after": "19:00",
                        "constraints": {"departure_time_after": "18:00"},
                    }
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="conflict"):
        normalize(raw, {"single_pass_planning": True})


@pytest.mark.parametrize(
    "metadata,value", [("field_sources", "proposal"), ("condition_strengths", "soft")]
)
def test_single_declared_constraint_aggregate_metadata_is_unambiguous(metadata, value):
    raw = {
        "tripplan": {
            "tasks": [
                {
                    "conditions": {"constraints": {"train_kind": "high_speed"}},
                    metadata: {"constraints": value},
                }
            ]
        }
    }
    before = deepcopy(raw)
    assert normalize(raw)["tripplan"]["tasks"][0][metadata] == {"train_kind": value}
    assert raw == before


def test_ambiguous_aggregate_metadata_error_has_path_expected_and_small_example():
    from trip_assistant.runtime.errors import ToolFailure, error_payload

    raw = {
        "tripplan": {
            "tasks": [
                {
                    "conditions": {
                        "constraints": {"train_kind": "high_speed", "seat_class": "二等座"}
                    },
                    "field_sources": {"constraints": "proposal"},
                }
            ]
        }
    }
    with pytest.raises(ToolFailure) as caught:
        normalize(raw)
    error = error_payload(caught.value)
    assert error["code"] == "INVALID_ARGUMENTS"
    field = error["details"]["fields"][0]
    assert field["field"] == "tripplan.tasks[0].field_sources.constraints"
    assert field["received"] == "proposal"
    assert field["expected"] and field["example"] == {
        "train_kind": "proposal",
        "seat_class": "proposal",
    }


def test_single_constraint_expansion_still_rejects_conflicting_metadata():
    raw = {
        "tripplan": {
            "tasks": [
                {
                    "conditions": {"constraints": {"train_kind": "high_speed"}},
                    "field_sources": {"constraints": "proposal", "train_kind": "user"},
                }
            ]
        }
    }
    with pytest.raises(ValueError, match="conflicting"):
        normalize(raw)


def test_feedback_does_not_echo_arbitrary_metadata_text():
    from trip_assistant.runtime.errors import ToolFailure, error_payload

    with pytest.raises(ToolFailure) as caught:
        normalize({"tripplan": {"field_sources": {"constraints": "private-free-form-secret"}}})
    details = error_payload(caught.value)["details"]
    assert details["fields"][0]["received_type"] == "str"
    assert "private-free-form-secret" not in str(details)


@pytest.mark.parametrize("value", [[], ["private-free-text"], None, 1])
def test_non_string_aggregate_metadata_gets_safe_structured_error(value):
    from trip_assistant.runtime.errors import ToolFailure

    with pytest.raises(ToolFailure) as caught:
        normalize(
            {
                "tripplan": {
                    "conditions": {"constraints": {"train_kind": "high_speed"}},
                    "field_sources": {"constraints": value},
                }
            }
        )
    assert caught.value.details["fields"][0]["received_type"] == type(value).__name__


@pytest.mark.asyncio
async def test_runtime_metadata_feedback_keeps_audit_and_has_no_side_effects(tmp_path):
    import json

    from support.native import NativeRuntimeFixture

    from evals.simulated_travel import SimulatedHighSpeedProvider

    train = SimulatedHighSpeedProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    args = {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "requires_hotel": False,
                    "conditions": {
                        "departure_date": "2026-10-21",
                        "constraints": {"train_kind": "high_speed", "seat_class": "二等座"},
                    },
                    "field_sources": {"constraints": "proposal"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
    }
    raw = deepcopy(args)

    def inspect(messages):
        assert "tripplan.tasks[0].field_sources.constraints" in json.dumps(
            messages, ensure_ascii=False
        )
        assert "example" in json.dumps(messages)
        return "参数错误，本轮没有保存计划。"

    runtime.model.script = [
        {"content": [{"type": "tool_use", "id": "bad", "name": "write_trip", "input": args}]},
        inspect,
    ]
    result = await runtime.run("后天重庆到上海单程")
    assert not train.calls and not runtime.trip_service.store.list_summaries()
    assert result["tool_results"][0]["error"]["details"]["fields"][0]["received"] == "proposal"
    call = next(e for e in runtime.sessions.read_events() if e["type"] == "tool_call")
    assert call["tool_calls"][0]["arguments"] == raw
