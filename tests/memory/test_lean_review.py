from copy import deepcopy

from support.native import NativeRuntimeFixture
from support.workflow import populated_workflow

from trip_assistant.memory.planning import review_payload


def test_preserved_committed_plan_does_not_imply_any_booking():
    from trip_assistant.memory.planning import reply_facts

    work = populated_workflow(quoted=True)
    work["committed"] = deepcopy(work)
    facts = reply_facts(work)
    assert facts["old_plan_preserved"] is True
    assert facts["booking_performed"] is False
    assert facts["reservation_performed"] is False


def test_failed_refresh_separates_preserved_plan_from_current_evidence(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    work = populated_workflow(quoted=True)
    work["committed"] = deepcopy(work) | {"revision": 7}
    task = work["tasks"][0]
    binding = next(v for v in work["results_by_query"].values() if v["domain"] == "train")
    binding["needs_revalidation"] = True
    work["pending_queries"] = [
        {"task_id": task["id"], "item": {"domain": "train"}, "reason": "query_failed"}
    ]
    saved = runtime.trip_service.store.save(work, expected_revision=None)
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "刷新车次"}, saved["id"]
    )
    facts = payload["reply_facts"]
    assert facts["old_plan_preserved"] is True
    assert facts["current_data_verified"] is False
    assert any(
        r["task_id"] == task["id"] and r["domain"] == "train" and r["reason"] == "query_failed"
        for r in facts["unverified_components"]
    )
    assert runtime.trip_service.store.load(saved["id"]) == saved

    saved["pending_queries"] = []
    saved["results_by_query"][binding["id"]].pop("needs_revalidation")
    saved = runtime.trip_service.store.save(saved, expected_revision=saved["revision"])
    refreshed = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "继续"}, saved["id"]
    )
    assert refreshed["reply_facts"]["unverified_components"] == []
    # No fresh query happened in this turn; lack of a failure cannot prove current availability.
    assert refreshed["reply_facts"]["current_data_verified"] is None


def test_review_deduplicates_single_source_but_preserves_multiple_sources():
    from trip_assistant.memory.planning import _review_view

    view = {
        "domain": "train",
        "query_id": "q",
        "result_revision": 1,
        "sources": {"s1": {"provider": "simulation:train", "fetched_at": "2026-10-17"}},
        "items": [{"id": "a", "source_id": "s1", "price_cny": "90", "remaining": None}],
        "has_more": True,
        "view_offset": 0,
    }
    before = deepcopy(view)
    compact = _review_view(view)
    assert "source_id" not in compact["items"][0]
    assert compact["sources"] == view["sources"]
    assert compact["items"][0]["price_cny"] == "90"
    assert compact["has_more"] and compact["view_offset"] == 0
    assert view == before
    view["sources"]["s2"] = {"provider": "other"}
    assert _review_view(view)["items"][0]["source_id"] == "s1"
    # An unrecognised source must not be rewritten as the sole listed source.
    view["sources"].pop("s2")
    view["items"][0]["source_id"] = "missing"
    assert _review_view(view)["items"][0]["source_id"] == "missing"


def test_review_omits_internal_duplicates_but_keeps_facts_and_history(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    workflow = populated_workflow(quoted=True)
    workflow["validation"] = {
        "valid": False,
        "signature": "internal-hash",
        "issues": [],
        "reconstructed_tasks": [],
        "validated_revisions": {},
    }
    saved = runtime.trip_service.store.save(workflow, expected_revision=None)
    plan = {"operation": "propose", "tasks": deepcopy(saved["tasks"])}
    runtime.sessions.record_calls(
        [
            {
                "id": "p",
                "name": "write_trip",
                "arguments": {
                    "request_kind": "planning",
                    "tripplan": plan,
                    "round_summary": "提出初始方案",
                },
            }
        ],
        "turn",
    )
    runtime.sessions.record_result(
        "p",
        {
            "name": "write_trip",
            "call_id": "p",
            "status": "partial",
            "trip_plan_id": saved["id"],
            "revision": saved["revision"],
        },
        "turn",
    )
    before = runtime.sessions.read_events()
    payload = review_payload(
        runtime.sessions, {"turn_id": "turn", "original_query": "规划上海往返"}, saved["id"]
    )
    snapshot = payload["tripplan"]
    assert (
        "created_at" not in snapshot and "original_query" not in snapshot and "id" not in snapshot
    )
    assert "signature" not in snapshot.get("validation", {})
    assert "tasks" not in payload["adjustment_history"][0]["changes"]
    assert len(snapshot["tasks"]) == len(saved["tasks"])
    assert snapshot["confirmed_conditions"] == saved["confirmed_conditions"]
    assert snapshot.get("policy_context") == saved.get("policy_context")
    assert runtime.trip_service.store.load(saved["id"]) == saved
    assert runtime.sessions.read_events() == before


def test_explicit_query_receipt_discards_earlier_cached_page_projection(tmp_path):
    from trip_assistant.memory.planning import _current_pages

    runtime = NativeRuntimeFixture(tmp_path)
    saved = runtime.trip_service.store.save(populated_workflow(quoted=True), expected_revision=None)
    binding = next(iter(saved["results_by_query"].values()))
    page = {
        "query_id": binding["id"],
        "result_revision": binding["result_revision"],
        "items": [],
        "view_offset": 5,
    }
    runtime.sessions.record_call({"id": "page", "name": "read", "arguments": {}}, "t")
    runtime.sessions.record_result("page", {"name": "read", "status": "ok", "data": page}, "t")
    assert _current_pages(runtime.sessions, "t", saved)
    runtime.sessions.record_call({"id": "q", "name": "query", "arguments": {}}, "t")
    runtime.sessions.record_result(
        "q",
        {
            "name": "query",
            "status": "ok",
            "trip_plan_id": saved["id"],
            "revision": saved["revision"],
        },
        "t",
    )
    assert _current_pages(runtime.sessions, "t", saved) == {}


def test_reading_later_page_keeps_original_candidates_available_to_model(tmp_path):
    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    work = populated_workflow(quoted=True)
    binding = next(iter(work["results_by_query"].values()))
    first = {
        "query_id": binding["id"],
        "result_revision": binding["result_revision"],
        "domain": "train",
        "items": binding["items"][:1],
        "view_offset": 0,
        "has_more": True,
    }
    work["last_candidate_views"] = [first]
    saved = runtime.trip_service.store.save(work, expected_revision=None)
    page = {
        **first,
        "items": [{**first["items"][0], "id": "later-candidate"}],
        "view_offset": 5,
        "has_more": False,
    }
    runtime.sessions.record_call({"id": "page", "name": "read", "arguments": {}}, "t")
    runtime.sessions.record_result("page", {"name": "read", "status": "ok", "data": page}, "t")
    payload = review_payload(
        runtime.sessions, {"turn_id": "t", "original_query": "规划"}, saved["id"]
    )
    trip = payload["tripplan"]
    assert trip["candidates"][0]["items"][0]["id"] == "later-candidate"
    assert trip["previous_candidate_pages"][0]["items"][0]["id"] == first["items"][0]["id"]
    assert len(trip["previous_candidate_pages"]) == 1
    assert runtime.trip_service.store.load(saved["id"]) == saved
