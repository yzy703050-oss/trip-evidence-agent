import multiprocessing

import pytest

from trip_assistant.storage.sessions import SessionStore


def event(**patch):
    return {
        "event_id": "e",
        "turn_id": "t",
        "type": "message",
        "scope": "session",
        "role": "user",
        "content": "hello",
        **patch,
    }


def test_append_is_idempotent_without_reading_history(tmp_path, monkeypatch):
    store = SessionStore(tmp_path, "alice", "s")
    first = store.append(event())

    def fail(*args, **kwargs):
        raise AssertionError("full history read")

    monkeypatch.setattr(store, "read_events", fail)
    monkeypatch.setattr(store, "read_runs", fail)
    assert store.append(event(content="changed")) == first
    assert store.append(event(event_id="second"))["seq"] == 2
    assert store.append_run({"turn_id": "t", "type": "model_call"})["run_seq"] == 1
    assert store.latest_seq() == 2
    assert SessionStore.list_sessions(tmp_path, "alice")[0]["title"] == "hello"


def test_state_patch_preserves_fields_and_rejects_stale_revision(tmp_path):
    from trip_assistant.storage.sqlite import StateConflictError

    a, b = [SessionStore(tmp_path, "alice", "s") for _ in range(2)]
    old = a.update_state({"summary": "old", "pending_question": "q"})
    b.update_state({"cleared_through_seq": 9}, remove_keys=("pending_question",))
    with pytest.raises(StateConflictError):
        a.update_state({"summary": "stale"}, expected_revision=old["state_revision"])
    a.add_active_workflow("one")
    b.add_active_workflow("two")
    state = b.read_state()
    assert state["summary"] == "old" and state["cleared_through_seq"] == 9
    assert "pending_question" not in state
    assert state["active_workflow_ids"] == ["one", "two"]


def test_tool_pairing_is_atomic_and_filtered(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    call = event(
        event_id="call",
        type="tool_call",
        scope="info",
        role="assistant",
        tool_calls=[{"id": "c", "name": "weather", "input": {}}],
    )
    store.append(call)
    assert store.pending_tool_calls()[0]["id"] == "c"
    with pytest.raises(ValueError, match="unique"):
        store.append({**call, "event_id": "duplicate"})
    with pytest.raises(ValueError, match="matching"):
        store.append(
            event(event_id="bad", type="tool_result", scope="wrong", role="tool", tool_call_id="c")
        )
    assert store.latest_seq() == 1
    store.append(
        event(event_id="result", type="tool_result", scope="info", role="tool", tool_call_id="c")
    )
    assert store.pending_tool_calls() == []
    assert store.read_events(after_seq=1, scope="info", event_type="tool_result")[0]["seq"] == 2
    assert store.read_events(scope="session", event_type="message") == []


def _append_process(root, label):
    store = SessionStore(root, "alice", "s")
    for n in range(8):
        store.append(event(event_id=f"{label}-{n}"))


def test_process_append_has_unique_sequences(tmp_path):
    SessionStore(tmp_path, "alice", "s")
    ctx = multiprocessing.get_context("spawn")
    workers = [
        ctx.Process(target=_append_process, args=(str(tmp_path), label)) for label in ("a", "b")
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    rows = SessionStore(tmp_path, "alice", "s").read_events()
    assert [row["seq"] for row in rows] == list(range(1, 17))
    assert len({row["event_id"] for row in rows}) == 16


def test_run_queries_and_metadata_do_not_decode_unrelated_records(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    store.append(event())
    store.append_run({"turn_id": "old", "type": "model_call"})
    wanted = store.append_run({"turn_id": "new", "type": "model_call"})
    with store.storage.transaction() as conn:
        conn.execute("UPDATE runs SET payload_json='invalid' WHERE turn_id='old'")
        conn.execute("UPDATE events SET payload_json='invalid'")
    assert store.read_runs(turn_id="new", event_type="model_call") == [wanted]
    assert SessionStore.list_sessions(tmp_path, "alice")[0]["title"] == "hello"
