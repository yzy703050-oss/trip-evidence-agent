"""Offline regression scenarios grouped by responsibility."""

import json
from copy import deepcopy
from pathlib import Path

from trip_assistant.trips.validation import check_workflow

# partial_selection_evidence


def saved_workflow():
    return json.loads(
        (Path(__file__).parent / "fixtures/shanghai_partial_selection.json").read_text(
            encoding="utf-8"
        )
    )["workflow"]


def reply_result(w):
    checked = check_workflow(w)
    return dict(workflow=w, validated_plan=checked, gaps=checked["issues"], status="partial")


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


# bounded_tool_results


def test_pagination_dedup_and_identity_fields_not_truncated():
    from trip_assistant.providers.views import build_tool_result_view

    data = {
        "status": "ok",
        "query": {"city": "上海"},
        "items": [
            {
                "id": str(i),
                "hotel_name": "酒店" * 1000,
                "stay_total_cny": "500",
                "check_in": "2026-10-13",
                "check_out": "2026-10-14",
                "photos": "large",
            }
            for i in range(12)
        ],
        "unverified_constraints": ["inventory"],
        "source": {"provider": "hotel"},
    }
    data["items"].append(deepcopy(data["items"][0]))
    view = build_tool_result_view("hotel", data, limits={"candidate_limit": 5, "offset": 5})
    assert view["candidate_total"] == 12 and view["has_more"]
    assert [i["id"] for i in view["items"]] == ["5", "6", "7", "8", "9"]
    assert view["items"][0]["stay_total_cny"] == "500"
    assert view["unverified_constraints"] == ["inventory"]
