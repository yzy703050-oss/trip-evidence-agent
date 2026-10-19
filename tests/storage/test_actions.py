import pytest

from trip_assistant.storage.sessions import SessionStore


def action(identity="t:q", **changes):
    return {
        "operation_id": identity,
        "kind": "query",
        "origin": "model",
        "payload": {"call_id": "q", "arguments": {}},
        **changes,
    }


def test_accept_is_atomic_idempotent_and_preserves_events(tmp_path, monkeypatch):
    from trip_assistant.storage.actions import ActionStore

    sessions = SessionStore(tmp_path, "a", "s")
    store = ActionStore(sessions)
    first = store.accept_many("t", [action()])
    assert store.accept_many("t", [action()]) == first
    with pytest.raises(ValueError):
        store.accept_many("t", [action(payload={"different": True})])
    original = sessions.append_in

    def fail(conn, event):
        original(conn, event)
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(sessions, "append_in", fail)
    with pytest.raises(RuntimeError):
        store.accept_many("t", [action("t:other")])
    assert store.get("t:other") is None
    assert len(sessions.read_events(event_type="action_state")) == 1


def test_batch_invalid_tail_rolls_back_and_claim_is_exclusive(tmp_path):
    from trip_assistant.storage.actions import ActionStore

    sessions = SessionStore(tmp_path, "a", "s")
    store = ActionStore(sessions)
    with pytest.raises(ValueError):
        store.accept_many("t", [action(), action("bad", origin="untrusted")])
    assert store.list("t") == [] and sessions.read_events() == []
    store.accept_many("t", [action()])
    assert store.claim("t:q") and not store.claim("t:q")
    sessions.append(
        {
            "event_id": "observation",
            "type": "execution_job",
            "turn_id": "t",
            "scope": "session",
            "content": {"status": "ok"},
        }
    )
    store.finish("t:q", "completed", "observation")
    assert not store.claim("t:q")
    assert store.get("t:q")["result_ref"] == "observation"
    with pytest.raises(ValueError):
        store.finish("t:q", "running", None)


def test_actions_and_results_cannot_cross_session_or_user(tmp_path):
    from trip_assistant.storage.actions import ActionStore

    sessions = SessionStore(tmp_path, "a", "s")
    store = ActionStore(sessions)
    store.accept_many("t", [action()])
    assert ActionStore(SessionStore(tmp_path, "a", "other")).get("t:q") is None
    assert ActionStore(SessionStore(tmp_path, "b", "s")).get("t:q") is None
    other = SessionStore(tmp_path, "a", "other")
    other.append({"event_id": "foreign", "type": "message", "turn_id": "t", "content": "x"})
    with pytest.raises(ValueError):
        store.finish("t:q", "completed", "foreign")
    assert store.get("t:q")["state"] == "queued"


def test_existing_database_gains_actions_without_changing_old_data(tmp_path):
    from support.workflow import populated_workflow

    from trip_assistant.storage.actions import ActionStore
    from trip_assistant.storage.trips import TripStore

    sessions = SessionStore(tmp_path, "a", "s")
    event = sessions.append(
        {
            "event_id": "old",
            "type": "message",
            "turn_id": "old-turn",
            "role": "user",
            "content": "历史问题",
        }
    )
    trips = TripStore(tmp_path, "a")
    work = populated_workflow(quoted=True)
    work["user_id"] = "a"
    saved = trips.save_working(work, expected_revision=None, operation_id="old-write")
    before = trips.load(saved["id"])
    receipt = trips.lookup_operation("old-write")
    # Only the isolated fixture database is changed to model the pre-feature schema.
    with sessions.storage.transaction() as conn:
        conn.execute("DROP TABLE execution_actions")
    reopened = SessionStore(tmp_path, "a", "s")
    assert ActionStore(reopened).list("old-turn") == []
    assert reopened.read_events() == [event]
    assert trips.load(saved["id"]) == before
    assert trips.lookup_operation("old-write") == receipt
