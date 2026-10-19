"""Session recovery and user boundaries through native stores and context projection."""

import json

import pytest
from support.native import make_application, record_user

from trip_assistant.memory.context import build_context
from trip_assistant.storage.preferences import LongTermMemory
from trip_assistant.storage.sessions import SessionStore


def finish(store, turn, content="reply", **extra):
    return store.append(
        {
            "type": "message",
            "scope": "session",
            "turn_id": turn,
            "role": "assistant",
            "content": content,
            "final": True,
            **extra,
        }
    )


def test_more_than_ten_small_turns_remain_visible(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    for i in range(12):
        finish(store, record_user(store, f"q{i}"), f"a{i}")
    snapshot = build_context(SessionStore(tmp_path, "alice", "s"))
    assert len(snapshot) == 24 and snapshot[0]["content"] == "q0"


def test_main_result_history_projects_reply_without_tool_payload(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    turn = record_user(store, "查票")
    raw = json.dumps(
        {
            "status": "completed",
            "final_answer": "已查到车票",
            "results": {"raw": "PRIVATE_TOOL_MARKER"},
        },
        ensure_ascii=False,
    )
    finish(store, turn, raw, metadata={"format": "main_result"})
    context = build_context(SessionStore(tmp_path, "alice", "s"))
    assert context[-1]["content"] == "已查到车票" and "PRIVATE_TOOL_MARKER" not in str(context)
    assert store.read_events()[-1]["content"] == raw


def test_session_resume_keeps_complete_turns_and_isolates_other_sessions(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    finish(store, record_user(store, "第一次问题"), "第一次回答")
    finish(store, record_user(store, "第二次问题"), "第二次回答")
    assert [r["content"] for r in build_context(SessionStore(tmp_path, "alice", "s1"))] == [
        "第一次问题",
        "第一次回答",
        "第二次问题",
        "第二次回答",
    ]
    assert build_context(SessionStore(tmp_path, "alice", "s2")) == []
    assert build_context(SessionStore(tmp_path, "bob", "s1")) == []


def test_tool_events_are_stored_but_child_audit_does_not_leak(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    finish(store, record_user(store, "q"), "a")
    store.append({"type": "agent_stage", "scope": "policy", "content": {"raw": "PRIVATE_CHILD"}})
    assert len(store.read_events()) == 3
    assert "PRIVATE_CHILD" not in str(build_context(store))


def test_pending_tool_call_survives_restart_and_result_requires_matching_id(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    turn = record_user(store, "查天气")
    store.record_call({"id": "q", "name": "query", "arguments": {"city": "上海"}}, turn)
    reopened = SessionStore(tmp_path, "alice", "s")
    assert reopened.pending_tool_calls()[0]["id"] == "q"
    with pytest.raises(ValueError):
        reopened.record_result("wrong", {"status": "ok"}, turn)
    with pytest.raises(ValueError):
        reopened.record_result("q", {"status": "ok"}, "wrong-turn")
    reopened.record_result("q", {"status": "ok"}, turn)
    assert not reopened.pending_tool_calls()
    assert build_context(reopened)[-1]["tool_call_id"] == "q"


def test_clear_context_does_not_delete_raw_log_and_persists_across_restart(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    finish(store, record_user(store, "old"), "old answer")
    before = store.read_events()
    store.clear_context()
    assert build_context(SessionStore(tmp_path, "alice", "s")) == []
    assert store.read_events() == before
    finish(store, record_user(store, "new"), "new answer")
    assert [r["content"] for r in build_context(store)] == ["new", "new answer"]


def test_clear_context_also_drops_old_compaction_summary(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    store.update_state({"summary": "PRIVATE_OLD"})
    store.clear_context()
    assert not build_context(store) and store.read_state()["summary"] == ""


def test_clear_abandons_pending_tool_calls_from_previous_task(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    turn = record_user(store, "q")
    store.record_call({"id": "c", "name": "query", "arguments": {}}, turn)
    store.clear_context()
    assert not store.pending_tool_calls()
    assert not build_context(store)
    with pytest.raises(ValueError):
        store.record_result("c", {"status": "ok"}, turn)


def test_manual_profile_value_is_visible_and_not_overwritten_by_agent(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.set_user_preference("hotel_brands", ["全季"])
    assert not memory.save_preference("hotel_brands", ["如家"])
    assert memory.get_preference("hotel_brands") == ["全季"]


def test_agent_append_helpers_respect_manual_profile_and_deletion(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.set_user_preference("hotel_brands", ["全季"])
    assert not memory.add_hotel_brand("汉庭")
    memory.delete_user_preference("hotel_brands")
    assert not memory.add_hotel_brand("汉庭") and memory.get_preference("hotel_brands") is None


def test_clear_history_removes_sessions_but_keeps_profile_after_restart(tmp_path):
    app = make_application("alice", "s", str(tmp_path))
    record_user(app.sessions, "q")
    app.preferences.set_user_preference("home_location", "上海")
    app.preferences.clear_history()
    reopened = make_application("alice", "s", str(tmp_path))
    assert reopened.preferences.get_preference("home_location") == "上海"
    assert not build_context(reopened.sessions)


@pytest.mark.asyncio
async def test_cli_records_user_before_main_and_one_final(tmp_path):
    from support.cli_session_resume import app_for

    cli = app_for(tmp_path)

    def answer(messages):
        assert cli.app.sessions.read_events()[-1]["content"] == "用户真实问题"
        return "回答"

    cli.raw_model.script = [answer]
    await cli.process_query("用户真实问题")
    events = cli.app.sessions.read_events(event_type="message")
    assert [(r["role"], r["content"]) for r in events] == [
        ("user", "用户真实问题"),
        ("assistant", "回答"),
    ]
    assert events[-1]["final"]


@pytest.mark.asyncio
async def test_bad_main_returns_safe_error_and_records_real_user(tmp_path):
    app = make_application("alice", "s", str(tmp_path))

    def fail(messages):
        raise ValueError("invalid response")

    app.raw_model.script = [fail]
    result = await app.run("用户真实问题")
    assert result["status"] == "partial" and result["stop_reason"] == "model_error"
    assert app.sessions.read_events()[0]["content"] == "用户真实问题"
    assert app.sessions.read_events(event_type="message", role="assistant")[-1]["final"]


@pytest.mark.parametrize("bad", [{"s1_transport": "{{id}}"}, None, 12])
def test_legacy_grouped_answer_object_cannot_enter_context_as_facts(tmp_path, bad):
    store = SessionStore(tmp_path, "alice", "s")
    tid = record_user(store, "旧行程")
    raw = json.dumps(
        {"final_answer": bad, "reply_template": {"groups": ["fake"]}, "raw": "PRIVATE_TOOL_MARKER"}
    )
    finish(store, tid, raw, metadata={"format": "main_result"})
    context = build_context(store)
    assert "按行程ID重新查看" in context[-1]["content"]
    assert "PRIVATE_TOOL_MARKER" not in str(context) and store.read_events()[-1]["content"] == raw
