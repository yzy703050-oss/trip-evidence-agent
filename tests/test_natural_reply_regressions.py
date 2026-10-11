from copy import deepcopy

import pytest
from test_cli_prose import SOURCE, domain, train

from utils.response_renderer import finalize_business_result


def logged_result():
    outgoing = train(
        train_number="D6",
        origin_station="上海",
        destination_station="北京",
        price_cny="338",
        departure_at="2026-10-18T21:15:00+08:00",
        arrival_at="2026-10-19T09:25:00+08:00",
    )
    returning = train(
        train_number="D9",
        origin_station="北京南",
        destination_station="上海南",
        price_cny="317",
        departure_at="2026-10-21T19:36:00+08:00",
        arrival_at="2026-10-22T08:00:00+08:00",
    )
    place = {
        "kind": "hotel_place",
        "hotel_name": "驿小栈青年酒店",
        "address": "青年路西里3号院",
        "source": SOURCE,
    }
    tasks = [
        dict(
            origin="上海",
            destination="北京",
            train=outgoing,
            hotel=place,
            schedule={
                "departure_at": outgoing["departure_at"],
                "arrival_at": outgoing["arrival_at"],
                "check_in": "2026-10-19",
                "check_out": "2026-10-21",
            },
        ),
        dict(
            origin="北京",
            destination="上海",
            train=returning,
            schedule={
                "departure_at": returning["departure_at"],
                "arrival_at": returning["arrival_at"],
            },
        ),
    ]
    return {
        "status": "completed",
        "workflow": {"id": "workflow_test"},
        "final_answer": (
            "火车与酒店安排已整理，共2段。已核实费用合计655元。"
            "酒店等未知费用另计，全程费用尚未完整核实。"
        ),
        "validated_plan": {
            "reconstructed_tasks": tasks,
            "budget": {
                "known_subtotal_cny": "655",
                "lines": [{}],
                "missing_categories": ["hotel"],
                "verified": False,
            },
        },
        "gaps": [{"message": "酒店地点不提供房价、空房或入住规则。"}],
    }


def test_logged_workflow_hides_sources_and_merges_duplicate_details():
    result = logged_result()
    before = deepcopy(result)
    text = finalize_business_result(result)["final_answer"]
    for expected in [
        "D6",
        "D9",
        "上海南",
        "北京南",
        "338元",
        "317元",
        "655元",
        "2026年10月19日 09:25",
        "2026年10月22日 08:00",
        "驿小栈青年酒店",
        "房价、空房和入住规则尚未核实",
    ]:
        assert expected in text
    for forbidden in [
        SOURCE["provider"],
        SOURCE["url"],
        SOURCE["fetched_at"],
        "+08:00",
        "参考链接",
        "查询时间",
    ]:
        assert forbidden not in text
    assert text.count("21:15") == 1
    assert text.count('从上海到北京') == 1
    assert text.count("655") == 1
    assert text.count("全程费用尚未完整核实") == 1
    assert result == before


@pytest.mark.parametrize(
    "domain_name,items",
    [
        ("train", [train()]),
        ("weather", [{"date": "2026-10-11", "description": "晴", "temp_c": 20, "source": SOURCE}]),
        (
            "guide",
            [
                {
                    "kind": "note",
                    "content": "博物馆开放时间待确认",
                    "verification": "needs_check",
                    "source": SOURCE,
                }
            ],
        ),
        (
            "web",
            [
                {
                    "title": "景点开放时间",
                    "snippet": "请提前预约",
                    "url": SOURCE["url"],
                    "source": SOURCE,
                }
            ],
        ),
    ],
)
def test_domain_answers_keep_facts_but_hide_source_metadata(domain_name, items):
    result = {"domain_results": {domain_name: domain(items)}}
    before = deepcopy(result)
    text = finalize_business_result(result)["final_answer"]
    assert SOURCE["provider"] not in text and SOURCE["url"] not in text
    assert SOURCE["fetched_at"] not in text
    assert result == before
    expected = {"train": "627.50", "weather": "20℃", "guide": "博物馆", "web": "请提前预约"}
    assert expected[domain_name] in text


def test_unknown_prices_and_stale_inventory_are_not_hidden():
    result = logged_result()
    result["validated_plan"]["reconstructed_tasks"][0]["train"].update(
        price_cny=None, availability="unknown", remaining=None, needs_revalidation=True
    )
    text = finalize_business_result(result)["final_answer"]
    assert "票价尚未核实" in text and "余票尚未核实" in text
    assert "重新核实" in text


def test_explicit_link_direct_reply_is_preserved():
    text = "官网链接：https://example.com/visit"
    assert finalize_business_result({"status": "ok", "final_answer": text})["final_answer"] == text


def test_saved_checkpoint_question_is_shown_without_empty_plan_diagnostics():
    result = logged_result()
    result.update(status='needs_input', final_answer='行程还未完成',
                  missing_fields=['origin', 'departure_date'])
    result['workflow']['checkpoint'] = {'question': '这次从哪个城市出发？'}
    result['gaps'].append({'code': 'invalid_draft', 'message': '草稿包含不支持的字段。'})
    before = deepcopy(result)
    assert finalize_business_result(result)['final_answer'] == '这次从哪个城市出发？'
    assert result == before


def test_needs_input_without_checkpoint_question_preserves_actual_question():
    result = logged_result()
    result.update(status='needs_input', final_answer='你想调整哪一段的酒店？')
    assert finalize_business_result(result)['final_answer'] == '你想调整哪一段的酒店？'
