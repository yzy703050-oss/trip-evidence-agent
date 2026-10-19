from copy import deepcopy

import pytest

from evals.manual.planning_reliability import assess


def policy_draft():
    return {
        "case": "business_policy_missing",
        "fixed_current_time": "2026-10-18T05:00:00+08:00",
        "result": {"status": "partial", "final_answer": "交通住宿草案已保留，企业政策尚未核实。"},
        "trips": [
            {
                "committed": None,
                "validation": {
                    "goal_assessment": {
                        "status": "partial",
                        "reason": "交通安排可用，政策未核实。",
                    },
                    "issues": [
                        {"code": "policy_unverified", "blocking": True},
                        {"code": "goal_not_satisfied", "blocking": True},
                    ],
                    "reconstructed_tasks": [
                        {
                            "train": {"departure_at": "2026-10-18T07:20:00+08:00"},
                            "hotel": {"hotel_name": "模拟酒店"},
                            "schedule": {
                                "arrival_at": "2026-10-18T18:50:00+08:00",
                                "check_in": "2026-10-18",
                            },
                            "content": [{"date": "2026-10-19", "time": "上午", "strength": "hard"}],
                        },
                        {"train": {"departure_at": "2026-10-19T12:20:00+08:00"}},
                    ],
                },
            }
        ],
    }


def test_policy_draft_accepts_additive_non_satisfied_assessment():
    checked = assess(policy_draft())
    assert checked["outcome"] == "expected_partial" and checked["failures"] == []


@pytest.mark.parametrize("fault", ["no_policy_gap", "other_blocker", "inconsistent_assessment"])
def test_goal_assessment_does_not_hide_real_benchmark_failures(fault):
    record = deepcopy(policy_draft())
    validation = record["trips"][0]["validation"]
    if fault == "no_policy_gap":
        validation["issues"] = [validation["issues"][1]]
    elif fault == "other_blocker":
        validation["issues"].append({"code": "query_pending", "blocking": True})
    else:
        validation["goal_assessment"]["status"] = "satisfied"
    assert "policy_draft_not_ready" in assess(record)["failures"]
