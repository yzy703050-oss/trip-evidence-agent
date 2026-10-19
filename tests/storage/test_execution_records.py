import pytest


def test_call_result_pairing_and_checkpoint(tmp_path):
    from trip_assistant.storage.sessions import SessionStore

    s = SessionStore(tmp_path, "alice", "s")
    call = {"id": "c", "name": "read", "arguments": {"resource": "preferences"}}
    s.record_call(call, "t")
    s.record_call(call, "t")
    assert len(s.pending_tool_calls()) == 1
    with pytest.raises(ValueError):
        s.record_result("missing", {"status": "ok"}, "t")
    s.record_result("c", {"status": "ok", "data": {}}, "t")
    assert s.pending_tool_calls() == []
    s.checkpoint_turn("t", {"planning_rounds_used": 2, "active_seconds": 400})
    assert s.read_state()["active_turn"]["active_seconds"] == 400


def test_evidence_preserves_history_and_validity(tmp_path):
    from trip_assistant.storage.evidence import EvidenceStore
    from trip_assistant.storage.sqlite import SQLiteStorage

    store = EvidenceStore(SQLiteStorage(tmp_path, "alice"))
    old = store.save(
        {
            "query": {"city": "上海"},
            "fetched_at": "2026-10-15T00:00:00+00:00",
            "items": [{"id": "h"}],
        },
        "hotel:sh",
    )
    assert store.get(old["query_id"], 1)["items"][0]["id"] == "h"
    assert (
        store.find_valid("hotel:sh", now="2026-10-15T00:10:00+00:00")["query_id"] == old["query_id"]
    )
    assert store.find_valid("hotel:sh", now="2026-10-16T00:10:00+00:00") is None
    newer = store.save(
        {"query": {"city": "上海"}, "fetched_at": "2026-10-15T01:00:00+00:00", "items": []},
        "hotel:sh",
    )
    assert newer["result_revision"] == 2 and store.get(old["query_id"], 1)["items"]


def test_duplicate_calls_and_results_require_identical_payloads(tmp_path):
    from trip_assistant.storage.sessions import SessionStore

    s = SessionStore(tmp_path, "alice", "s")
    c = {"id": "c", "name": "read", "arguments": {"resource": "preferences"}}
    s.record_call(c, "t")
    with pytest.raises(ValueError):
        s.record_call({**c, "name": "write_trip"}, "t")
    result = {"status": "ok", "data": {"x": 1}}
    first = s.record_result("c", result, "t")
    assert s.record_result("c", result, "t") == first
    with pytest.raises(ValueError):
        s.record_result("c", {"status": "error"}, "t")
