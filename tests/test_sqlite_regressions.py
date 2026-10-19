import json
from types import SimpleNamespace

import pytest
from support.native import make_application, record_user
from support.workflow import populated_workflow

from trip_assistant.storage.sessions import SessionStore
from trip_assistant.storage.sqlite import StateConflictError


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completed", "partial", "hard_conflict"])
async def test_cli_persists_only_slim_final_and_keeps_full_result(tmp_path, kind):
    from support.cli_session_resume import app_for

    from trip_assistant.memory.context import build_context

    app = app_for(tmp_path)
    if kind == "completed":
        app.raw_model.script = ["行程问答"]
    else:
        task = {"origin": "上海", "destination": "北京"}
        if kind == "hard_conflict":
            task.update(
                conditions={"departure_date": "2026-10-20", "arrival_date": "2026-10-19"},
                condition_strengths={"departure_date": "hard", "arrival_date": "hard"},
            )
        app.raw_model.script = [
            SimpleNamespace(
                content=[
                    {
                        "type": "tool_use",
                        "id": "draft",
                        "name": "write_trip",
                        "input": {"request_kind": "planning", "tripplan": {"tasks": [task]}},
                    }
                ]
            ),
            "等待补充或更正条件。",
        ]
    await app.process_query("单程去北京")
    result = app.last_result
    event = app.app.sessions.read_events(event_type="message", role="assistant")[-1]
    assert event["content"] == result["final_answer"]
    assert not {"tasks", "validation", "tool_results", "results_by_query"} & set(event)
    if kind != "completed":
        assert event["trip_plan_id"] == result["trip"]["trip_plan_id"]
        assert app.app.trips.load(event["trip_plan_id"])["working"]
    assert build_context(app.app.sessions)[-1]["content"] == result["final_answer"]


def test_clear_history_and_recreate_session_cannot_reuse_state_revision(tmp_path):
    memory = make_application("alice", "s", str(tmp_path))
    state = memory.sessions.update_state({"summary": "old"})
    memory.preferences.clear_history()
    fresh = SessionStore(tmp_path, "alice", "s")
    fresh.update_state({"summary": "new"})
    with pytest.raises(StateConflictError):
        memory.sessions.update_state(
            {"summary": "stale"}, expected_revision=state["state_revision"]
        )
    assert fresh.read_state()["summary"] == "new"


def test_delete_all_rejects_old_async_state_revision(tmp_path):
    memory = make_application("alice", "s", str(tmp_path))
    old = memory.sessions.read_state()
    memory.preferences.delete_all()
    fresh = SessionStore(tmp_path, "alice", "s")
    with pytest.raises(StateConflictError):
        fresh.update_state({"summary": "stale"}, expected_revision=old["state_revision"])


def test_completed_migration_does_not_depend_on_old_identity(tmp_path):
    from trip_assistant.storage.identity import user_storage_key

    directory = tmp_path / user_storage_key("alice")
    directory.mkdir()
    identity = directory / "identity.json"
    identity.write_text(json.dumps({"user_id": "alice", "version": 1}), encoding="utf-8")
    memory = make_application("alice", "s", str(tmp_path))
    memory.preferences.set_user_preference("seat", "二等座")
    identity.write_text("obsolete damaged source", encoding="utf-8")
    assert (
        make_application("alice", "s", str(tmp_path)).preferences.get_preference("seat") == "二等座"
    )


def test_tool_result_cannot_commit_after_context_clear(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    store.append(
        {
            "type": "tool_call",
            "turn_id": "t",
            "scope": "info",
            "tool_calls": [{"id": "c", "name": "weather"}],
        }
    )
    store.clear_context()
    with pytest.raises(ValueError, match="matching"):
        store.append({"type": "tool_result", "turn_id": "t", "scope": "info", "tool_call_id": "c"})
    assert store.latest_seq() == 1


def test_statistics_does_not_decode_full_workflow_or_audit(tmp_path):
    memory = make_application("alice", "s", str(tmp_path))
    workflow = populated_workflow()
    workflow["status"] = "completed"
    memory.trips.save(workflow, expected_revision=None)
    record_user(memory.sessions, "q")
    with memory.preferences.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET content_json='cannot decode'")
        conn.execute("UPDATE events SET payload_json='cannot decode'")
    statistics = memory.preferences.get_statistics()
    assert statistics["total_trips"] == 1 and statistics["total_messages"] == 1


def test_summary_identity_and_revision_mismatch_are_rejected(tmp_path):
    memory = make_application("alice", "s", str(tmp_path))
    memory.trips.save(populated_workflow(), expected_revision=None)
    summary = memory.trips.list_summaries()[0]
    summary["revision"] += 7
    with memory.preferences.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET summary_json=?", (json.dumps(summary),))
    with pytest.raises(ValueError, match="summary"):
        memory.trips.list_summaries()


def test_database_unavailable_never_falls_back_to_legacy(tmp_path):
    source = tmp_path / "alice.json"
    source.write_text(
        json.dumps({"user_id": "alice", "preferences": {"seat": "二等座"}}), encoding="utf-8"
    )
    memory = make_application("alice", "s", str(tmp_path))
    before = source.read_bytes()
    memory.preferences.storage.path.write_bytes(b"not a database")
    with pytest.raises(Exception, match="database"):
        memory.preferences.get_preference("seat")
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_prior_session_summary_is_not_read_or_restored_after_clear(tmp_path):
    from trip_assistant.memory.context import build_context

    old = make_application("alice", "old", str(tmp_path))
    old.sessions.update_state({"summary": "old hotel facts"})
    old.sessions.clear_context()
    reopened = SessionStore(tmp_path, "alice", "old")
    assert not build_context(reopened)
    current = make_application("alice", "new", str(tmp_path))
    current.raw_model.script = ["请说明本次需求。"]
    await current.run("酒店")
    assert "old hotel facts" not in str(current.raw_model.messages)
    assert old.sessions.read_state()["session_summary"] == ""


def test_cli_resume_reports_sqlite_error_and_keeps_binding(tmp_path):
    from support.cli_session_resume import app_for

    app = app_for(tmp_path)
    before = app.app
    before.preferences.storage.path.write_bytes(b"not a database")
    assert not app.resume_session() and app.app is before
    assert "暂时无法恢复" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_workflow_sqlite_failure_returns_storage_error(tmp_path, monkeypatch):
    import sqlite3

    memory = make_application("alice", "s", str(tmp_path))

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(memory.trips, "save", fail)
    memory.raw_model.script = [
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "plan",
                    "name": "write_trip",
                    "input": {
                        "request_kind": "planning",
                        "tripplan": {"tasks": [{"origin": "上海", "destination": "北京"}]},
                    },
                }
            ]
        ),
        "存储失败，行程尚未保存。",
    ]
    result = await memory.run("单程去北京")
    assert result["status"] == "partial"
    assert result["tool_results"][0]["status"] == "error"
    assert "disk full" in result["tool_results"][0]["reason"]
    assert memory.trips.list_summaries() == []
