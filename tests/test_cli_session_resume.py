"""Preserved CLI session behavior against the native application."""

import pytest
from support.cli_session_resume import app_for
from support.native import make_application, record_user
from support.workflow import populated_workflow

from trip_assistant.storage.sessions import SessionStore
from trip_assistant.tools.read import build_read_handler


def previous(root, user="alice", session="old"):
    app = make_application(user, session, storage_path=str(root))
    record_user(app.sessions, "我要去上海玩然后再去北京")
    app.sessions.update_state({"summary": "旧摘要"})
    return app


def test_startup_always_creates_session_without_prompt(tmp_path, monkeypatch):
    previous(tmp_path)
    app = app_for(tmp_path)
    monkeypatch.setattr(
        "trip_assistant.cli.Prompt.ask", lambda *a, **k: pytest.fail("启动不得选择会话")
    )
    first = app.choose_session_id()
    assert len(first) == 32 and first != app.choose_session_id()
    assert not app.console.file.getvalue()


def test_list_sessions_uses_topics_and_current_user_only(tmp_path):
    previous(tmp_path)
    previous(tmp_path, user="bob", session="foreign")
    rows = SessionStore.list_sessions(tmp_path, "alice")
    assert [r["session_id"] for r in rows] == ["old"]
    assert rows[0]["title"] == "我要去上海玩然后再去北京"
    assert rows[0]["last_user_message"] == rows[0]["title"]
    assert "+08:00" in rows[0]["updated_at"]


def test_resume_cancel_keeps_current_binding(tmp_path, monkeypatch):
    previous(tmp_path)
    app = app_for(tmp_path)
    before = app.app
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *a, **k: "")
    assert not app.resume_session() and app.app is before and app.session_id == "current"


@pytest.mark.asyncio
async def test_resume_rebinds_messages_agents_and_telemetry_without_model_call(
    tmp_path, monkeypatch
):
    previous(tmp_path)
    app = app_for(tmp_path)
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *a, **k: "1")
    assert app.resume_session() and app.session_id == "old"
    assert app.app.sessions.read_runs() == [] and not app.raw_model.messages
    assert app.app.main.model is app.app.model
    await app.process_query("继续")
    assert len([r for r in app.app.sessions.read_runs() if r["type"] == "model_call"]) == 1
    assert SessionStore(tmp_path, "alice", "current").read_runs() == []
    assert app.app.sessions.read_events(event_type="message", role="user")[-1]["content"] == "继续"


def test_resume_construction_failure_is_atomic(tmp_path, monkeypatch):
    previous(tmp_path)
    app = app_for(tmp_path)
    before = (app.app, app.raw_model, app.providers)
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *a, **k: "1")

    def fail(*a, **k):
        raise OSError("读取失败")

    monkeypatch.setattr("trip_assistant.cli.create_application", fail)
    assert not app.resume_session()
    assert (app.app, app.raw_model, app.providers) == before and app.session_id == "current"


@pytest.mark.asyncio
async def test_new_session_does_not_get_prior_active_tasks(tmp_path):
    old = previous(tmp_path)
    work = populated_workflow()
    work["status"] = "partial"
    saved = old.trips.save(work, expected_revision=None)
    old.sessions.add_active_workflow(saved["id"])
    current = make_application("alice", "new", str(tmp_path))
    assert current.sessions.read_state().get("active_workflow_ids", []) == []
    read = build_read_handler(current.trips, current.evidence)
    result = await read({"resource": "trip_summaries", "filters": {"city": "北京"}}, {})
    assert result["data"]["summaries"][0]["id"] == saved["id"]
    assert make_application("bob", "new", str(tmp_path)).trips.list_summaries() == []


def test_empty_resume_list_does_not_ask(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    monkeypatch.setattr(
        "trip_assistant.cli.Prompt.ask", lambda *a, **k: pytest.fail("空列表不应询问")
    )
    assert not app.resume_session()
    output = app.console.file.getvalue()
    assert "直接开始聊天" in output and "以后" in output


def test_new_conversation_is_available_after_reopening(tmp_path):
    app = app_for(tmp_path)
    app.app.preferences.set_user_preference("home_location", "重庆")
    record_user(app.app.sessions, "我明天想去上海玩")
    reopened = make_application("alice", "another", str(tmp_path))
    assert reopened.preferences.get_preference("home_location") == "重庆"
    assert [
        (r["session_id"], r["title"]) for r in SessionStore.list_sessions(tmp_path, "alice")
    ] == [("current", "我明天想去上海玩")]
    assert SessionStore.list_sessions(tmp_path, "bob") == []


def test_corrupt_session_restore_fails_without_rebinding(tmp_path, monkeypatch):
    old = previous(tmp_path)
    with old.sessions.storage.transaction() as conn:
        conn.execute("UPDATE sessions SET state_json='invalid' WHERE id='old'")
    app = app_for(tmp_path)
    before = app.app
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *a, **k: "1")
    assert not app.resume_session() and app.app is before and app.session_id == "current"


@pytest.mark.asyncio
async def test_plain_new_plan_does_not_inject_prior_origin_into_main(tmp_path):
    old = previous(tmp_path)
    record_user(old.sessions, "上海出发，10月18日去北京")
    app = app_for(tmp_path)
    app.raw_model.script = ["这次从哪里出发？"]
    await app.process_query("我要去上海玩然后再去北京")
    assert "上海出发" not in str(app.raw_model.messages)
    assert "旧摘要" not in str(app.raw_model.messages)
    assert not old.sessions.read_runs()
