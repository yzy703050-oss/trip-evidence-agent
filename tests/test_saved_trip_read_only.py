from copy import deepcopy

import pytest
from workflow_support import populated_workflow


def test_saved_candidate_pages_and_history_do_not_mutate_plan():
    from agents.workflow_reads import read_saved_trip

    w = populated_workflow()
    before = deepcopy(w)
    task = w["tasks"][0]
    result = read_saved_trip(
        w,
        {
            "expected_workflow_revision": w["revision"],
            "task_id": task["id"],
            "domain": "train",
            "limit": 1,
        },
    )
    assert result["pages"][0]["candidate_total"] >= 1
    assert len(result["pages"][0]["items"]) == 1
    assert w == before
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == task["id"] and r["domain"] == "train"
    )
    row["needs_revalidation"] = True
    assert not read_saved_trip(w, {"task_id": task["id"], "domain": "train"})["pages"]
    assert (
        read_saved_trip(w, {"task_id": task["id"], "domain": "train", "include_history": True})[
            "pages"
        ][0]["valid"]
        is False
    )


def test_stale_read_and_unbounded_page_are_rejected():
    from agents.workflow_reads import read_saved_trip

    w = populated_workflow()
    with pytest.raises(ValueError):
        read_saved_trip(w, {"expected_workflow_revision": w["revision"] + 1})
    with pytest.raises(ValueError):
        read_saved_trip(w, {"limit": 10000})


def test_all_saved_candidates_are_split_into_five_five_two_without_mutation():
    from agents.workflow_reads import read_saved_trip

    w = populated_workflow()
    t = w["tasks"][0]
    row = next(
        r
        for r in w["results_by_query"].values()
        if r["task_id"] == t["id"] and r["domain"] == "hotel"
    )
    row["items"] = [dict(row["items"][0], id=f"h{i}") for i in range(12)]
    before = deepcopy(w)
    result = read_saved_trip(w, {"task_id": t["id"], "domain": "hotel", "all": True})
    assert [len(p["items"]) for p in result["pages"]] == [5, 5, 2]
    assert [p["view_offset"] for p in result["pages"]] == [0, 5, 10]
    assert w == before
