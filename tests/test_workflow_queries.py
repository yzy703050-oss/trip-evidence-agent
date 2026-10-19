import pytest
from support.workflow import workflow
from support.workflow_queries import put_train, train_result

from trip_assistant.trips.evidence import query_views, record_query, resolve_selection


def test_queries_do_not_overwrite_the_route_and_five_is_only_a_view():
    w = workflow()
    records = [put_train(w, i) for i in range(3)]
    assert len({r["id"] for r in records}) == 3
    assert len(w["results_by_query"]) == 3
    assert w["confirmed_conditions"]["start_date"] == "2026-10-11"
    assert len(records[0]["items"]) == 8
    views = query_views(w, w["tasks"][0]["id"])
    assert len(views[0]["items"]) == 5
    assert len(query_views(w, w["tasks"][0]["id"], offset=5)[0]["items"]) == 3


def test_selection_requires_matching_task_query_revision_and_id():
    w = workflow()
    r = put_train(w)
    other = put_train(w, 1)
    t = w["tasks"][0]
    ref = dict(query_id=r["id"], result_revision=1, candidate_id=r["items"][0]["id"])
    assert resolve_selection(w, t["id"], 1, ref)["price_cny"] == "100"
    for bad in [
        dict(ref, query_id=other["id"]),
        dict(ref, result_revision=2),
        dict(ref, candidate_id="fake"),
    ]:
        with pytest.raises(ValueError):
            resolve_selection(w, t["id"], 1, bad)


def test_failed_refresh_retains_facts_and_marks_reconfirmation():
    w = workflow()
    r = put_train(w)
    task = w["tasks"][0]
    failed = dict(status="error", items=[], source=None, fetched_at=None)
    saved = record_query(
        w, task["id"], r["parameters"], {}, failed, {}, domain="train", refresh=True
    )
    assert saved["id"] == r["id"] and saved["items"] == r["items"]
    assert saved["result_revision"] == 1 and saved["needs_revalidation"] is True
    fresh = record_query(
        w, task["id"], r["parameters"], {}, train_result(task), {}, domain="train", refresh=True
    )
    assert fresh["result_revision"] == 2
    assert fresh["needs_revalidation"] is False


def test_juhe_does_not_invent_arrival_day():
    from support.juhe_train_provider import search

    data, _ = search()
    item = data.items[0]
    assert item["departure_at"] == "2026-10-09T18:04:00+08:00"
    assert item["arrival_at"] is None
