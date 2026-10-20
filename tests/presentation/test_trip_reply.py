from copy import deepcopy

import pytest


def receipt():
    return {
        "status": "completed",
        "operation": "select",
        "trip_plan_id": "trip",
        "revision": 4,
        "tasks": [
            {
                "id": "out",
                "origin": "重庆",
                "destination": "上海",
                "content": [{"date": "2026-10-21", "time": "晚上", "purpose": "滨江慢走"}],
            },
            {"id": "back", "origin": "上海", "destination": "重庆", "content": []},
        ],
        "validation": {
            "valid": True,
            "issues": [],
            "reconstructed_tasks": [
                {
                    "task_id": "out",
                    "train": {
                        "train_number": "G9002",
                        "seat_class": "二等座",
                        "departure_at": "2026-10-21T08:20:00+08:00",
                        "arrival_at": "2026-10-21T15:20:00+08:00",
                        "price_cny": "230",
                    },
                    "hotel": {"hotel_name": "全季", "kind": "hotel_place"},
                    "schedule": {"check_in": "2026-10-21", "check_out": "2026-10-23"},
                },
                {
                    "task_id": "back",
                    "train": {
                        "train_number": "G9004",
                        "seat_class": "二等座",
                        "departure_at": "2026-10-23T10:20:00+08:00",
                        "arrival_at": "2026-10-23T17:20:00+08:00",
                        "price_cny": "290",
                    },
                    "schedule": {},
                },
            ],
        },
    }


def test_reply_reads_selected_facts_and_total_without_rewriting_plan():
    from trip_assistant.presentation.trip_reply import render_trip_reply

    result = receipt()
    before = deepcopy(result)
    answer = render_trip_reply({"explanation": "会议结束时间未知，返程安排在次日。"}, result)
    assert "G9002" in answer and "15:20" in answer and "14:20" not in answer
    assert "230元" in answer and "290元" in answer and "520元" in answer
    assert "滨江慢走" in answer and "房价" in answer and "未核实" in answer
    assert result == before


def test_content_view_reads_only_actually_saved_activity_and_no_transport_total():
    from trip_assistant.presentation.trip_reply import render_trip_reply

    result = receipt()
    spec = {"tasks": {"out": ["content"]}}
    assert "滨江慢走" in render_trip_reply(spec, result)
    result["tasks"][0]["content"][0]["purpose"] = "就近散步"
    result["operation"] = "amend"
    answer = render_trip_reply(spec, result)
    assert "就近散步" in answer and "滨江慢走" not in answer
    assert "G9002" not in answer and "520元" not in answer


def test_unknown_fare_is_not_added_as_zero_or_presented_as_complete_total():
    from trip_assistant.presentation.trip_reply import render_trip_reply

    result = receipt()
    result["validation"]["reconstructed_tasks"][1]["train"]["price_cny"] = None
    answer = render_trip_reply({}, result)
    assert "票价未核实" in answer and "已知交通费用" in answer
    assert "交通小计：230元" not in answer


@pytest.mark.parametrize(
    "reply",
    [
        {"tasks": {"missing": ["train"]}},
        {"tasks": {"out": ["train", "train"]}},
        {"tasks": {"out": ["unsupported"]}},
        {"explanation": "G9002在14:20到达"},
    ],
)
def test_reply_scope_and_literal_fact_format_rejected(reply):
    from trip_assistant.presentation.trip_reply import validate_trip_reply

    with pytest.raises(ValueError):
        validate_trip_reply(reply, {"operation": "select"}, receipt())


def test_ready_reply_uses_bound_result_ahead_of_legacy_prose():
    from trip_assistant.runtime.state import ExecutionState

    result = receipt()
    result.update(name="write_trip", call_id="select", rendered_answer="G9002，15:20抵达")
    calls = [{"id": "select", "arguments": {"final_answer": "14:20抵达"}}]
    assert ExecutionState.ready_reply(calls, [result]) == "G9002，15:20抵达"


def test_default_schema_shows_reply_but_legacy_input_still_works():
    from trip_assistant.tools.registry import build_registry

    registry = build_registry({})
    public = registry["write_trip"].schema["function"]["parameters"]["properties"]
    assert "reply" in public and "final_answer" not in public
    args = {
        "request_kind": "planning",
        "tripplan": {"operation": "amend", "trip_plan_id": "t", "expected_revision": 1},
        "final_answer": "旧调用仍有效。",
    }
    assert registry["write_trip"].validate(args)["final_answer"] == "旧调用仍有效。"


def test_all_unknown_fares_do_not_become_zero_cost():
    from trip_assistant.presentation.trip_reply import render_trip_reply

    result = receipt()
    for task in result["validation"]["reconstructed_tasks"]:
        task["train"]["price_cny"] = None
    answer = render_trip_reply({}, result)
    assert "交通费用待核实" in answer and "0元" not in answer


def test_explicit_reply_scope_cannot_refer_to_route_tasks_being_replaced():
    from trip_assistant.presentation.trip_reply import validate_trip_reply

    with pytest.raises(ValueError):
        validate_trip_reply(
            {"tasks": {"out": ["content"]}},
            {"operation": "amend", "route_tasks": [{"origin": "重庆"}]},
            receipt(),
        )


def test_ready_reply_keeps_all_bound_plans_in_same_batch():
    from trip_assistant.runtime.state import ExecutionState

    first = {**receipt(), "name": "write_trip", "call_id": "a", "rendered_answer": "第一份计划"}
    second = {**receipt(), "name": "write_trip", "call_id": "b", "rendered_answer": "第二份计划"}
    calls = [{"id": "a", "arguments": {"reply": {}}}, {"id": "b", "arguments": {"reply": {}}}]
    answer = ExecutionState.ready_reply(calls, [first, second])
    assert "第一份计划" in answer and "第二份计划" in answer


def test_bound_reply_preserves_related_weather_numbers_and_memory_receipt():
    from trip_assistant.presentation.trip_reply import append_related_facts

    results = [
        {
            "name": "query",
            "data": [
                {
                    "domain": "weather",
                    "status": "ok",
                    "query": {"city": "上海", "date": "2026-10-21"},
                    "items": [
                        {
                            "date": "2026-10-21",
                            "description": "晴",
                            "min_temp_c": 18,
                            "max_temp_c": 24,
                        }
                    ],
                }
            ],
        },
        {"name": "write_memory", "status": "ok"},
    ]
    answer = append_related_facts("已保存行程。", results)
    assert "18" in answer and "24" in answer and "晴" in answer and "上海" in answer
    assert "偏好" in answer and "更新" in answer


def test_related_fact_refresh_shows_latest_result_without_old_failure():
    from trip_assistant.presentation.trip_reply import append_related_facts

    query = {"city": "上海"}
    results = [
        {
            "name": "query",
            "data": [{"domain": "weather", "query": query, "status": "error", "message": "旧失败"}],
        },
        {
            "name": "query",
            "data": [
                {
                    "domain": "weather",
                    "query": query,
                    "status": "ok",
                    "items": [{"temp_c": 24, "description": "晴"}],
                }
            ],
        },
    ]
    answer = append_related_facts("行程。", results)
    assert "24" in answer and "旧失败" not in answer


def test_bound_display_does_not_add_fixed_simulation_or_booking_disclaimers():
    from trip_assistant.presentation.trip_reply import render_trip_reply

    result = receipt()
    result["validation"]["reconstructed_tasks"][0]["train"]["source"] = {
        "provider": "simulation:train"
    }
    before = deepcopy(result)
    answer = render_trip_reply({}, result)
    assert "模拟数据" not in answer and "未预订" not in answer
    assert "G9002" in answer and result == before
