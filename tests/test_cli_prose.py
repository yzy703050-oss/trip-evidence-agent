"""Business replies read as prose without losing validated facts or limitations."""

import io
import json
from copy import deepcopy

import pytest
from rich.console import Console
from workflow_support import populated_workflow

from agents.workflow_guard import check_workflow
from cli import TripEvidenceCLI
from utils.response_renderer import finalize_business_result

SOURCE = {
    "provider": "测试来源",
    "url": "https://example.com/query",
    "fetched_at": "2026-10-10T09:00:00+08:00",
}


def display(result):
    app = TripEvidenceCLI()
    stream = io.StringIO()
    app.console = Console(file=stream, width=5000, color_system=None)
    before = deepcopy(result)
    final_result = finalize_business_result(result)
    app._display_results(final_result)
    assert stream.getvalue().strip() == final_result["final_answer"]
    assert result == before
    return stream.getvalue().strip()


def train(**changes):
    return {
        "id": "train-1",
        "train_number": "G25",
        "origin_station": "北京南",
        "destination_station": "上海虹桥",
        "departure_time": "09:00",
        "arrival_time": "14:00",
        "seat_class": "二等座",
        "price_cny": "627.50",
        "availability": "available",
        "remaining": 3,
        "source": SOURCE,
        "url": SOURCE["url"],
        **changes,
    }


def domain(items, **changes):
    return {
        "status": "ok",
        "items": items,
        "source": SOURCE,
        "query": {},
        "missing_fields": [],
        **changes,
    }


@pytest.mark.parametrize(
    "answer",
    [
        {"answer": "你已保存的长期居住城市是上海。"},
        json.dumps({"answer": "你已保存的长期居住城市是上海。"}, ensure_ascii=False),
        '```json\n{"answer":"你已保存的长期居住城市是上海。"}\n```',
    ],
)
def test_structured_final_answer_becomes_prose(answer):
    text = display({"status": "ok", "final_answer": answer})
    assert text == "你已保存的长期居住城市是上海。"


def test_plain_direct_answer_is_not_repeated_or_expanded():
    assert (
        display({"status": "ok", "final_answer": "你好，有什么出行安排需要帮忙？"})
        == "你好，有什么出行安排需要帮忙？"
    )


@pytest.mark.parametrize(
    "answer",
    [
        '酒店品牌偏好已设置为：["全季", "汉庭"]。',
        {"preferences": {"hotel_brands": ["全季", "汉庭"], "home_location": "上海"}},
    ],
)
def test_preference_values_are_readable_without_json_arrays(answer):
    text = display({"status": "ok", "final_answer": answer})
    assert "全季" in text and "汉庭" in text
    for internal in ("[", "]", "{", "}", "hotel_brands", "home_location"):
        assert internal not in text


def test_nested_embedded_result_keeps_prose_and_normal_links():
    text = display(
        {
            "final_answer": '查询结果：{"answer":{"message":"居住城市是上海"}}；参考[指南](https://example.com/guide)。'
        }
    )
    assert "居住城市是上海" in text and "[指南](https://example.com/guide)" in text
    assert "{" not in text and "}" not in text and "message" not in text


def test_train_reply_retains_quote_source_times_and_stock_without_field_dump():
    text = display(
        {
            "final_answer": "火车查询：1 个已取得结果；ok。",
            "domain_results": {"train": domain([train()])},
        }
    )
    for expected in (
        "G25",
        "北京南",
        "上海虹桥",
        "09:00",
        "14:00",
        "二等座",
        "627.50",
        "元",
        "3",
        "有票",
        SOURCE["url"],
        SOURCE["fetched_at"],
    ):
        assert expected in text
    for internal in ('"price_cny"', "availability", "available", "source", "ok", "{", "}", "None"):
        assert internal not in text
    assert "\n" not in text


def test_unknown_train_price_and_stock_are_explicit():
    text = display(
        {
            "domain_results": {
                "train": domain([train(price_cny=None, availability="unknown", remaining=None)])
            }
        }
    )
    assert "票价尚未核实" in text and "余票尚未核实" in text
    assert "None" not in text and "unknown" not in text and "无票" not in text


def test_hotel_place_remains_a_location_reference_with_unknown_booking_details():
    place = {
        "id": "place-1",
        "kind": "hotel_place",
        "hotel_name": "全季酒店",
        "address": "示例路1号",
        "city": "上海",
        "district": "浦东",
        "location": "121.5,31.2",
        "source": SOURCE,
    }
    text = display({"domain_results": {"hotel": domain([place])}})
    assert "全季酒店" in text and "示例路1号" in text
    assert "地点参考" in text and "房价、空房和入住规则尚未核实" in text
    assert "unknown" not in text and "hotel_place" not in text


def test_hotel_quote_preserves_stay_total_and_fee_basis():
    hotel = {
        "id": "hotel-1",
        "hotel_name": "全季",
        "room_type": "标准间",
        "check_in": "2026-10-11",
        "check_out": "2026-10-13",
        "guests": 1,
        "stay_total_cny": "800.00",
        "fees_included": True,
        "availability": "available",
        "cancellation": None,
        "source": SOURCE,
        "url": SOURCE["url"],
    }
    text = display({"domain_results": {"hotel": domain([hotel])}})
    assert "800.00" in text and "住宿总价" in text and "含费用" in text
    assert "2026-10-11" in text and "2026-10-13" in text
    assert "退改规则尚未核实" in text and "True" not in text


def test_workflow_schedule_and_revalidation_render_as_sentences():
    workflow = populated_workflow()
    checked = check_workflow(workflow)
    checked["reconstructed_tasks"][0]["train"]["needs_revalidation"] = True
    text = display(
        {
            "status": "partial",
            "workflow": workflow,
            "validated_plan": checked,
            "stop_reason": "main_decision_limit",
            "gaps": checked["issues"],
        }
    )
    assert "北京" in text and "杭州" in text and "2026-10-15" in text
    assert "重新核实" in text and "尚未完整核实" in text and "决策" in text
    for internal in (
        "task_id",
        "check_in",
        "departure_at",
        "main_decision_limit",
        "False",
        "None",
        "{",
        "}",
    ):
        assert internal not in text
    assert "\n" not in text


def test_missing_fields_and_general_error_are_natural_language():
    text = display({"status": "error", "missing_fields": ["origin", "check_in"]})
    assert "出发城市" in text and "入住日期" in text and "未能完成" in text
    assert "origin" not in text and "check_in" not in text and "{" not in text


def test_guide_and_weather_are_sourced_readable_facts():
    fact = {
        "kind": "note",
        "content": "博物馆开放时间待确认",
        "verification": "needs_check",
        "source": SOURCE,
    }
    weather = {
        "date": "2026-10-11",
        "description": "晴",
        "min_temp_c": 18,
        "max_temp_c": 24,
        "source": SOURCE,
    }
    text = display(
        {
            "domain_results": {
                "guide": domain([fact]),
                "weather": domain([weather], query={"city": "上海", "date": "2026-10-11"}),
            }
        }
    )
    assert "博物馆开放时间待确认" in text and "待核实" in text
    assert (
        "上海" in text and "18" in text and "24" in text and "℃" in text and SOURCE["url"] in text
    )
    assert "needs_check" not in text and '{"' not in text


def test_web_results_keep_title_snippet_and_link():
    text = display(
        {
            "domain_results": {
                "web": domain(
                    [
                        {
                            "title": "上海出行",
                            "snippet": "地铁出行说明",
                            "url": "https://example.com/shanghai",
                            "source": SOURCE,
                        }
                    ]
                )
            }
        }
    )
    assert "上海出行" in text and "地铁出行说明" in text and "https://example.com/shanghai" in text


def test_legacy_itinerary_uses_guarded_activities_in_prose():
    text = display(
        {
            "itinerary": {
                "summary": "车票299元",
                "itinerary": {
                    "daily_plans": [
                        {
                            "day": 1,
                            "date": "2026-10-11",
                            "activities": [
                                {
                                    "location_ref": "suggestion:city_walk",
                                    "description": "今日开放",
                                    "time": "10:00",
                                }
                            ],
                        }
                    ]
                },
            }
        }
    )
    assert "市内漫步" in text and "2026-10-11" in text and "未报价" in text
    assert "299" not in text and "今日开放" not in text and "daily_plans" not in text
