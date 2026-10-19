from copy import deepcopy

import pytest

from trip_assistant.presentation.facts import finalize


def test_recovered_policy_and_query_preflight_errors_do_not_keep_committed_trip_partial():
    from trip_assistant.presentation.facts import turn_status

    results = [
        {"name": "query_policy", "status": "rejected"},
        {"name": "query", "status": "rejected"},
        {"name": "query_policy", "status": "ok", "data": {"status": "ok"}},
        {
            "name": "query",
            "status": "ok",
            "data": [
                {
                    "status": "ok",
                    "domain": "train",
                    "query": {"origin": "重庆", "destination": "上海"},
                }
            ],
        },
        receipt(),
    ]
    before = deepcopy(results)
    assert turn_status(results, None) == "completed"
    assert results == before


def test_unresolved_policy_or_actual_independent_query_failure_still_makes_turn_partial():
    from trip_assistant.presentation.facts import turn_status

    assert (
        turn_status([{"name": "query_policy", "status": "rejected"}, receipt()], None) == "partial"
    )
    results = [
        {
            "name": "query",
            "status": "partial",
            "data": [
                {
                    "status": "error",
                    "domain": "weather",
                    "query": {"city": "上海"},
                }
            ],
        },
        {
            "name": "query",
            "status": "ok",
            "data": [
                {
                    "status": "ok",
                    "domain": "train",
                    "query": {"origin": "重庆", "destination": "上海"},
                }
            ],
        },
        receipt(),
    ]
    assert turn_status(results, None) == "partial"


def receipt(**changes):
    return {
        "name": "write_trip",
        "status": "completed",
        "trip_plan_id": "trip",
        "revision": 2,
        "validation": {"valid": True, "issues": []},
        **changes,
    }


@pytest.mark.parametrize(
    "answer",
    [
        "查询结果已过期未能保存。",
        "尚未预订成功，酒店地点信息不代表空房。",
        "当前方案尚未完成保存。",
        "当前已有可用安排。",
    ],
)
def test_reply_words_never_change_execution_facts_or_trigger_replacement(answer):
    results = [
        {
            "name": "write_trip",
            "status": "rejected",
            "error": {"code": "EVIDENCE_EXPIRED", "details": {}},
        }
    ]
    checked = finalize(answer, results)
    assert checked["answer"] == answer and checked["method"] == "main_agent"
    assert checked["execution_facts"]["saved"] is False
    assert checked["execution_facts"]["errors"][0]["error"]["code"] == "EVIDENCE_EXPIRED"


@pytest.mark.parametrize(
    "changes",
    [
        {"trip_plan_id": None},
        {"revision": None},
        {"revision": 0},
        {"revision": True},
        {"validation": {}},
        {"validation": {"valid": False}},
    ],
)
def test_incomplete_or_invalid_save_receipt_cannot_certify_completion(changes):
    checked = finalize("保留模型的自然表达。", [receipt(**changes)])
    assert checked["execution_facts"]["saved"] is False
    assert checked["issues"] == ["invalid_save_receipt"]


def test_latest_failed_write_is_distinct_from_previous_saved_plan():
    results = [
        receipt(),
        {"name": "write_trip", "status": "error", "error": {"code": "TOOL_ERROR", "details": {}}},
    ]
    before = deepcopy(results)
    checked = finalize("本次修改没有完成，原方案保留。", results)
    assert not checked["execution_facts"]["saved"]
    assert results == before and results[0]["revision"] == 2


def test_real_receipt_certifies_save_without_requiring_text_checklist():
    answer = "已确认采用这个行程，尚未预订。"
    checked = finalize(answer, [receipt()])
    assert checked["answer"] == answer and checked["issues"] == []
    assert checked["execution_facts"]["saved"] is True
    assert checked["execution_facts"]["revision"] == 2


def test_internal_metadata_and_history_are_not_save_or_price_evidence():
    checked = finalize(
        "酒店可作为地点参考。",
        [
            {
                "name": "query",
                "status": "ok",
                "revision": 500,
                "constraints": {"hotel_max_nightly_cny": 500},
                "data": [{"items": [{"hotel_name": "参考酒店", "kind": "hotel_place"}]}],
            },
        ],
        context={"review_evidence": {"adjustment_history": [{"price_cny": 500}]}},
    )
    facts = checked["execution_facts"]
    assert facts["saved"] is False and facts["trip_plan_id"] is None
    assert "price_cny" not in facts and "availability" not in facts


def test_structured_validation_problem_remains_visible_without_mutating_source():
    result = receipt(
        status="partial",
        validation={
            "valid": False,
            "issues": [
                {"code": "hard_date_conflict", "blocking": True},
            ],
        },
    )
    before = deepcopy(result)
    checked = finalize("时间冲突，当前未能完成安排。", [result])
    assert checked["execution_facts"]["validation_issues"] == before["validation"]["issues"]
    assert checked["execution_facts"]["saved"] is False and result == before
