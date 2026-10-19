from copy import deepcopy

import pytest

from trip_assistant.storage.trips import TripStore
from trip_assistant.trips.conditions import create_workflow


def workflow():
    return create_workflow(
        {"tasks": [{"origin": "重庆", "destination": "上海", "requires_hotel": False}]}, {}
    )


@pytest.mark.parametrize(
    "old,reason", [("needs_input", "needs_input"), ("paused", "paused"), ("cancelled", "cancelled")]
)
def test_legacy_state_and_stop_guards_preserve_identity(old, reason):
    from trip_assistant.trips.state import derive_dispatchability, normalize_workflow_states

    w = workflow()
    w["status"] = old
    w["tasks"][0]["status"] = "needs_input"
    before = deepcopy(w)
    result = normalize_workflow_states(w)
    assert result["status"] == "running" and result["tasks"][0]["status"] == "pending"
    assert result["id"] == before["id"] and result["revision"] == before["revision"]
    assert result["stop_reason"] == reason
    if old != "needs_input":
        assert not derive_dispatchability(result, result["tasks"][0]["id"], [])["executable"]
    assert w == before


def test_inflight_unknown_completion_prevents_replay_and_invalidation_keeps_history():
    from trip_assistant.trips.state import derive_dispatchability, invalidate_query_binding

    w = workflow()
    t = w["tasks"][0]
    records = [{"request_id": "r", "task_id": t["id"], "task_revision": 1, "started_at": "now"}]
    assert not derive_dispatchability(w, t["id"], records)["executable"]
    result = invalidate_query_binding(
        {"status": "ready", "current_query_ref": "q", "history_refs": []}, "date_changed"
    )
    assert result["status"] == "empty" and result["current_query_ref"] is None
    assert result["history_refs"] == ["q"]


def test_unknown_state_is_rejected_and_store_indexes_stop_reason(tmp_path):
    from trip_assistant.trips.state import normalize_workflow_states

    w = workflow()
    w["status"] = "unknown"
    with pytest.raises(ValueError):
        normalize_workflow_states(w)
    w["status"] = "cancelled"
    store = TripStore(str(tmp_path), "a")
    saved = store.save(w, expected_revision=None)
    assert saved["status"] == "running"
    assert store.list_summaries()[0]["stop_reason"] == "cancelled"
    assert store.list_active() == []


def test_legacy_querying_preserves_unknown_request_before_normalizing():
    from trip_assistant.trips.state import derive_dispatchability, normalize_workflow_states

    w = workflow()
    t = w["tasks"][0]
    t["query_containers"] = {
        "train": {"status": "querying", "request_id": "paid-r", "started_at": "now"}
    }
    normalized = normalize_workflow_states(w)
    assert normalized["tasks"][0]["query_containers"]["train"]["status"] == "empty"
    assert derive_dispatchability(normalized, t["id"], normalized["execution_plan"])[
        "inflight"
    ] == ["paid-r"]


def test_legacy_completed_with_invalid_selection_requires_revalidation():
    from support.workflow import populated_workflow

    from trip_assistant.trips.state import normalize_workflow_states

    w = populated_workflow()
    w["status"] = "completed"
    w["tasks"][0]["status"] = "validated"
    w["tasks"][0]["draft_plan"]["train_selection"]["candidate_id"] = "missing"
    result = normalize_workflow_states(w)
    assert result["status"] == "running"
    assert result["stop_reason"] == "validation_required"
    assert result["tasks"][0]["status"] == "draft"
    assert not result["validation"]["valid"]
    assert result["id"] == w["id"] and result["revision"] == w["revision"]
    assert w["status"] == "completed"


def test_legacy_completed_with_valid_evidence_keeps_completed():
    from support.workflow import populated_workflow

    from trip_assistant.trips.state import normalize_workflow_states

    w = populated_workflow()
    w["status"] = "completed"
    assert normalize_workflow_states(w)["status"] == "completed"
