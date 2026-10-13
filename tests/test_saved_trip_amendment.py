from copy import deepcopy

import pytest
from workflow_support import populated_workflow


def test_existing_candidates_reselection_preserves_queries_and_other_tasks():
    from agents.workflow_amendments import apply_amendment_patch
    from agents.workflow_batches import initialize_query_containers

    w = populated_workflow()
    initialize_query_containers(w)
    for old in w["tasks"]:
        old["status"] = "validated"
    w["status"] = "completed"
    task = w["tasks"][0]
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == task["id"] and r["domain"] == "train"
    )
    task["query_containers"]["train"].update(status="ready", current_query_ref=row["id"])
    before = deepcopy(w)
    result = apply_amendment_patch(
        w,
        {
            "workflow_id": w["id"],
            "expected_workflow_revision": w["revision"],
            "task_id": task["id"],
            "expected_task_revision": task["revision"],
            "components": ["train"],
            "condition_changes": {},
            "selection_refs": {
                "train": {
                    "query_id": row["id"],
                    "result_revision": row["result_revision"],
                    "candidate_id": row["items"][0]["id"],
                }
            },
        },
        {},
    )
    assert not result["query_requests"]
    assert result["candidate_workflow"]["tasks"][1:] == before["tasks"][1:]
    assert (
        result["candidate_workflow"]["tasks"][0]["query_containers"]["train"]["status"] == "ready"
    )
    assert w == before


def test_changed_dates_only_request_missing_hotel_evidence_and_reject_stale_patch():
    from agents.workflow_amendments import apply_amendment_patch

    w = populated_workflow()
    t = w["tasks"][0]
    patch = {
        "workflow_id": w["id"],
        "expected_workflow_revision": w["revision"],
        "task_id": t["id"],
        "expected_task_revision": t["revision"],
        "components": ["hotel"],
        "condition_changes": {"check_in": "2026-10-25", "check_out": "2026-10-27"},
        "selection_refs": {},
    }
    result = apply_amendment_patch(w, patch, {})
    # Place evidence is reusable across dates; a quote-required request needs actual date evidence.
    assert all(r["domain"] == "hotel" for r in result["query_requests"])
    patch["expected_workflow_revision"] += 1
    with pytest.raises(ValueError):
        apply_amendment_patch(w, patch, {})


def test_unauthorized_or_fabricated_field_never_applied():
    from agents.workflow_amendments import apply_amendment_patch

    w = populated_workflow()
    t = w["tasks"][0]
    patch = {
        "workflow_id": w["id"],
        "expected_workflow_revision": w["revision"],
        "task_id": t["id"],
        "expected_task_revision": t["revision"],
        "components": ["hotel"],
        "condition_changes": {"price_cny": "1"},
        "selection_refs": {},
    }
    with pytest.raises(ValueError):
        apply_amendment_patch(w, patch, {})


def test_amendment_expired_quote_is_gap_and_history_kept_on_rebinding():
    from agents.workflow_amendments import apply_amendment_patch, prepare_saved_trip_amendment

    w = populated_workflow()
    t = w["tasks"][0]
    view = prepare_saved_trip_amendment(
        w,
        {
            "task_id": t["id"],
            "current_time": "2026-10-11T10:00:00+08:00",
            "evidence_max_age_seconds": 3600,
        },
    )
    assert "train" in view["missing_domains"]
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == t["id"] and r["domain"] == "train"
    )
    patch = {
        "workflow_id": w["id"],
        "expected_workflow_revision": w["revision"],
        "task_id": t["id"],
        "expected_task_revision": t["revision"],
        "components": ["train"],
        "condition_changes": {},
        "selection_refs": {},
    }
    candidate = apply_amendment_patch(w, patch, {})["candidate_workflow"]
    assert candidate["results_by_query"][row["id"]]["history"][-1]["task_revision"] == t["revision"]


def test_changed_hotel_keyword_filters_full_cache_before_bounding():
    from agents.workflow_amendments import prepare_saved_trip_amendment

    w = populated_workflow()
    t = w["tasks"][0]
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == t["id"] and r["domain"] == "hotel"
    )
    row["items"] = [
        dict(row["items"][0], id=f"h{i}", hotel_name="汉庭" if i == 7 else "全季") for i in range(8)
    ]
    t["conditions"]["hotel_keywords"] = "汉庭"
    view = prepare_saved_trip_amendment(w, {"task_id": t["id"]})
    hotels = next(v for v in view["candidate_views"] if v["domain"] == "hotel")
    assert [i["id"] for i in hotels["items"]] == ["h7"]


def test_hotel_only_patch_does_not_refresh_expired_unaffected_train():
    from agents.workflow_amendments import apply_amendment_patch

    w = populated_workflow()
    t = w["tasks"][0]
    patch = {
        "workflow_id": w["id"],
        "expected_workflow_revision": w["revision"],
        "task_id": t["id"],
        "expected_task_revision": t["revision"],
        "components": ["hotel"],
        "condition_changes": {},
        "selection_refs": {},
    }
    result = apply_amendment_patch(w, patch, {"current_time": "2026-10-11T10:00:00+08:00"})
    assert all(q["domain"] == "hotel" for q in result["query_requests"])
    assert (
        result["candidate_workflow"]["tasks"][0]["draft_plan"]["train_selection"]
        == t["draft_plan"]["train_selection"]
    )
