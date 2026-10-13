"""Regress available-but-unselected evidence in the user's actual Shanghai run."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from agents.workflow_batches import check_batch_submission
from agents.workflow_guard import check_workflow
from utils.workflow_reply import build_reply_facts


def saved_workflow():
    return json.loads(
        (Path(__file__).parent / "fixtures/shanghai_partial_selection.json").read_text(
            encoding="utf-8"
        )
    )["workflow"]


def reply_result(w):
    checked = check_workflow(w)
    return dict(workflow=w, validated_plan=checked, gaps=checked["issues"], status="partial")


def test_actual_null_selections_report_available_candidates_without_picking():
    w = saved_workflow()
    before = deepcopy(w)
    values = build_reply_facts(reply_result(w))["values"]
    assert "未选定" in values["s1_hotel_gap"]
    assert "未选定" in values["s2_train_gap"]
    assert "没查到" in values["s1_train_gap"]
    assert w == before
    assert w["tasks"][1]["draft_plan"]["train_selection"] is None


@pytest.mark.parametrize("domain", ["train", "hotel"])
@pytest.mark.parametrize(
    "case,expected",
    [
        ("empty", "没有返回"),
        ("error", "没查到"),
        ("excluded", "不满足"),
        ("stale", "当前有效"),
        ("refresh_failed", "重新核实"),
    ],
)
def test_missing_selection_describes_current_evidence(domain, case, expected):
    w = saved_workflow()
    task = w["tasks"][1 if domain == "train" else 0]
    row = w["results_by_query"]["return_train" if domain == "train" else "hotel"]
    if case == "empty":
        row["items"] = []
    elif case == "error":
        row.update(status="error", items=[])
    elif case == "excluded":
        task["rejected_candidates"] = {domain: [i["id"] for i in row["items"]]}
    elif case == "stale":
        row["task_revision"] = 0
    else:
        row.update(status="partial", needs_revalidation=True)
    values = build_reply_facts(reply_result(w))["values"]
    key = ("s2_" if domain == "train" else "s1_") + domain + "_gap"
    assert expected in values[key]
    assert "未选定" not in values[key]


def test_candidate_filter_and_current_container_control_gap():
    w = saved_workflow()
    row = w["results_by_query"]["return_train"]
    row["constraints"]["train_max_total_cny"] = 1
    assert "不满足" in build_reply_facts(reply_result(w))["values"]["s2_train_gap"]
    row["constraints"] = {}
    failed = deepcopy(row)
    failed.update(id="new_failed", status="error", items=[])
    w["results_by_query"]["new_failed"] = failed
    w["tasks"][1]["query_ids"].append("new_failed")
    w["tasks"][1]["query_containers"]["train"]["current_query_ref"] = "new_failed"
    assert "没查到" in build_reply_facts(reply_result(w))["values"]["s2_train_gap"]


@pytest.mark.parametrize("case", ["paged_past_end", "empty_container"])
def test_pagination_and_invalidated_binding_do_not_misstate_evidence(case):
    w = saved_workflow()
    if case == "paged_past_end":
        w["results_by_query"]["return_train"]["view_offset"] = 1000
        expected = "未选定"
    else:
        w["tasks"][1]["query_containers"]["train"]["current_query_ref"] = None
        expected = "当前有效"
    assert expected in build_reply_facts(reply_result(w))["values"]["s2_train_gap"]


def select_available(w):
    tasks = deepcopy(w["tasks"])
    for task, domain, query_id in [
        (tasks[0], "hotel", "hotel"),
        (tasks[1], "train", "return_train"),
    ]:
        row = w["results_by_query"][query_id]
        task["draft_plan"][domain + "_selection"] = dict(
            query_id=query_id,
            result_revision=row["result_revision"],
            candidate_id=row["items"][0]["id"],
        )
    return dict(
        action="commit_batch",
        expected_workflow_revision=w["revision"],
        allow_repair=False,
        draft_tasks=[
            dict(task_id=t["id"], task_revision=t["revision"], draft_plan=t["draft_plan"])
            for t in tasks
        ],
    )


def test_hotel_and_return_survive_unknown_outbound_without_claiming_complete():
    w = saved_workflow()
    submitted = check_batch_submission(w, select_available(w))
    checked = submitted["validation"]
    assert checked["valid"] is False
    assert [i["code"] for i in checked["issues"] if i["blocking"]] == ["train_selection_missing"]
    text = build_reply_facts(reply_result(submitted["workflow"]))["fallback"]
    assert "登巴客栈" in text and "K71" in text
    assert "酒店房价、空房" in text and "随实际抵达调整" in text
    assert "返程为暂定选择" in text and "衔接" in text
    assert "s2_train_gap" not in build_reply_facts(reply_result(submitted["workflow"]))["values"]


@pytest.mark.parametrize("requirement", ["quote", "budget", "known_conflict"])
def test_retention_does_not_bypass_hard_requirements(requirement):
    w = saved_workflow()
    if requirement == "quote":
        w["tasks"][0]["conditions"]["hotel_quote_required"] = True
    elif requirement == "budget":
        w["tasks"][0]["conditions"]["constraints"] = {"hotel_max_nightly_cny": 500}
    else:
        w["tasks"][0]["conditions"]["check_out"] = "2026-10-17"
        w["tasks"][0]["field_sources"]["check_out"] = "user"
        w["tasks"][0]["condition_strengths"]["check_out"] = "hard"
        w["tasks"][0]["draft_plan"]["schedule"]["check_out"] = "2026-10-17"
        w["tasks"][0]["conditions"]["nights"] = 3
    checked = check_batch_submission(w, select_available(w))["validation"]
    codes = {i["code"] for i in checked["issues"] if i["blocking"]}
    assert (
        "hotel_price_unknown" in codes
        if requirement != "known_conflict"
        else "departure_before_previous_checkout" in codes
    )
