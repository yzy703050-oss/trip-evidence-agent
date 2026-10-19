import json
import multiprocessing

import pytest

from trip_assistant.storage.identity import user_storage_key
from trip_assistant.storage.sqlite import SQLiteStorage, metadata


def legacy(root):
    directory = root / user_storage_key("alice")
    directory.mkdir(parents=True)
    (directory / "identity.json").write_text(
        json.dumps({"user_id": "alice", "version": 1}), encoding="utf-8"
    )
    (directory / "profile.json").write_text(
        json.dumps(
            {
                "user_id": "alice",
                "preferences": [{"type": "seat", "value": "二等座", "source": "user"}],
                "statistics": {"total_queries": 7},
            }
        ),
        encoding="utf-8",
    )
    workflow = {
        "user_id": "alice",
        "id": "w",
        "revision": 12,
        "status": "partial",
        "tasks": [{"id": "t", "revision": 3, "results": {"hotel": [{"id": "h"}]}}],
        "checkpoint": {"kind": "missing", "expected_workflow_revision": 12},
    }
    (directory / "workflows").mkdir()
    (directory / "workflows/w.json").write_text(json.dumps(workflow), encoding="utf-8")
    session = directory / "sessions/s"
    session.mkdir(parents=True)
    event = {
        "user_id": "alice",
        "session_id": "s",
        "seq": 4,
        "event_id": "e",
        "turn_id": "t",
        "scope": "session",
        "type": "message",
        "role": "user",
        "content": "继续行程",
        "timestamp": "2026-10-10T10:00:00",
    }
    (session / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    (session / "state.json").write_text(
        json.dumps(
            {
                "user_id": "alice",
                "session_id": "s",
                "summary": "old",
                "summary_covered_through_seq": 3,
            }
        ),
        encoding="utf-8",
    )
    return directory, workflow, event


def source_bytes(directory):
    return {
        p.relative_to(directory): p.read_bytes()
        for p in directory.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl"}
    }


def test_import_preserves_bytes_and_identity(tmp_path):
    directory, workflow, event = legacy(tmp_path)
    before = source_bytes(directory)
    db = SQLiteStorage(tmp_path, "alice")
    db.ensure_ready()
    assert source_bytes(directory) == before
    with db.connection() as conn:
        assert (
            json.loads(conn.execute("SELECT content_json FROM trip_plans").fetchone()[0])
            == workflow
        )
        assert json.loads(conn.execute("SELECT payload_json FROM events").fetchone()[0]) == event
        assert (
            json.loads(conn.execute("SELECT state_json FROM sessions").fetchone()[0])[
                "summary_covered_through_seq"
            ]
            == 3
        )
        assert conn.execute("SELECT last_seq FROM sessions").fetchone()[0] == 4
        assert metadata(conn, "migration_complete") == "1"


def test_repeat_start_does_not_reimport(tmp_path):
    directory, _, _ = legacy(tmp_path)
    db = SQLiteStorage(tmp_path, "alice")
    db.ensure_ready()
    with db.transaction() as conn:
        conn.execute("UPDATE preferences SET value_json=?", (json.dumps("new"),))
    (directory / "profile.json").write_text("corrupted obsolete source", encoding="utf-8")
    SQLiteStorage(tmp_path, "alice").ensure_ready()
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert json.loads(conn.execute("SELECT value_json FROM preferences").fetchone()[0]) == "new"


def test_import_failure_rolls_back_all_and_retries(tmp_path, monkeypatch):
    directory, _, _ = legacy(tmp_path)
    before = source_bytes(directory)
    db = SQLiteStorage(tmp_path, "alice")
    with db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON trip_plans "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        db.ensure_ready()
    with db.connection() as conn:
        for table in (
            "preferences",
            "profiles",
            "events",
            "sessions",
            "trip_plans",
            "migration_sources",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert metadata(conn, "migration_complete") is None
    assert source_bytes(directory) == before
    with db.transaction() as conn:
        conn.execute("DROP TRIGGER fail")
    db.ensure_ready()


def test_truncated_tail_readonly_but_complete_corruption_fails(tmp_path):
    directory, _, _ = legacy(tmp_path)
    path = directory / "sessions/s/events.jsonl"
    original = path.read_bytes() + b'{"seq":5'
    path.write_bytes(original)
    db = SQLiteStorage(tmp_path, "alice")
    db.ensure_ready()
    assert path.read_bytes() == original
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert (
            "truncated"
            in conn.execute(
                "SELECT manifest_json FROM migration_sources WHERE path=?", (str(path.resolve()),)
            ).fetchone()[0]
        )
    other = tmp_path / "other"
    directory, _, _ = legacy(other)
    (directory / "sessions/s/events.jsonl").write_text("invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="events.jsonl.*1"):
        SQLiteStorage(other, "alice").ensure_ready()


def test_source_change_aborts(tmp_path, monkeypatch):
    from trip_assistant.storage import migration as storage_migration

    directory, _, _ = legacy(tmp_path)
    original = storage_migration.verify_sources

    def changed(sources):
        (directory / "profile.json").write_text("changed", encoding="utf-8")
        return original(sources)

    monkeypatch.setattr(storage_migration, "verify_sources", changed)
    db = SQLiteStorage(tmp_path, "alice")
    with pytest.raises(ValueError, match="changed"):
        db.ensure_ready()
    with db.connection() as conn:
        assert metadata(conn, "migration_complete") is None
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_unowned_legacy_session_is_not_claimed(tmp_path):
    source = tmp_path / "alice/sessions/foreign/events.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("invalid\n", encoding="utf-8")
    db = SQLiteStorage(tmp_path, "alice")
    db.ensure_ready()
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def _first_open(root):
    db = SQLiteStorage(root, "alice")
    db.ensure_ready()
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_two_processes_first_migrate_once(tmp_path):
    directory, _, _ = legacy(tmp_path)
    before = source_bytes(directory)
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_first_open, args=(str(tmp_path),)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert source_bytes(directory) == before


def test_migration_retains_runs_pending_tools_and_active_identity(tmp_path):
    directory, workflow, _ = legacy(tmp_path)
    event_path = directory / "sessions/s/events.jsonl"
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "user_id": "alice",
                    "session_id": "s",
                    "seq": 5,
                    "turn_id": "t",
                    "scope": "info",
                    "type": "tool_call",
                    "tool_calls": [{"id": "call", "name": "hotel", "arguments": {"city": "上海"}}],
                }
            )
            + "\n"
        )
    run = {
        "user_id": "alice",
        "session_id": "s",
        "run_seq": 9,
        "turn_id": "t",
        "type": "model_call",
        "input_tokens": 42,
    }
    (directory / "sessions/s/runs.jsonl").write_text(json.dumps(run) + "\n", encoding="utf-8")
    state_path = directory / "sessions/s/state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["active_workflow_ids"] = ["w"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before = source_bytes(directory)
    from support.native import make_application

    memory = make_application("alice", "s", str(tmp_path))
    from trip_assistant.trips.state import normalize_workflow_states

    assert [
        memory.trips.load(identity)
        for identity in memory.sessions.read_state().get("active_workflow_ids", [])
    ] == [normalize_workflow_states(workflow)]
    assert memory.sessions.read_runs() == [run]
    assert memory.sessions.pending_tool_calls()[0]["id"] == "call"
    assert memory.sessions.append_run({"turn_id": "new", "type": "query_run"})["run_seq"] == 10
    memory.sessions.record_result("call", {"hotels": []}, "t")
    assert memory.sessions.pending_tool_calls() == []
    assert source_bytes(directory) == before


def test_owned_old_directory_history_clear_marker_is_retained(tmp_path):
    directory = tmp_path / "alice"
    (directory / "workflows").mkdir(parents=True)
    (directory / "profile.json").write_text(
        json.dumps({"user_id": "alice", "preferences": []}), encoding="utf-8"
    )
    (directory / "trips.json").write_text(
        json.dumps({"user_id": "alice", "trip_history": [{"destination": "old"}]}), encoding="utf-8"
    )
    (directory / "workflows/.history-state.json").write_text(
        json.dumps(
            {"cleared_before": "2026-11-01T00:00:00+00:00", "imported_sources": ["trips.json"]}
        ),
        encoding="utf-8",
    )
    before = source_bytes(directory)
    from trip_assistant.storage.preferences import LongTermMemory

    memory = LongTermMemory("alice", str(tmp_path))
    assert memory.get_trip_history() == []
    with memory.storage.connection() as conn:
        assert metadata(conn, "history_cleared_before") == "2026-11-01T00:00:00+00:00"
    assert source_bytes(directory) == before
