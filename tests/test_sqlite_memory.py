import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from support.workflow import populated_workflow

from trip_assistant.storage.preferences import LongTermMemory
from trip_assistant.storage.trips import TripStore


def test_workflow_summary_failure_preserves_previous_revision(tmp_path, monkeypatch):
    import trip_assistant.storage.index as module

    store = TripStore(tmp_path, "alice")
    saved = store.save(populated_workflow(), expected_revision=None)
    summaries = store.list_summaries()

    def fail(value):
        raise ValueError("summary serialization failure")

    monkeypatch.setattr(module, "workflow_summary", fail)
    with pytest.raises(ValueError, match="summary serialization"):
        store.save(saved, expected_revision=saved["revision"])
    assert store.load(saved["id"]) == saved
    assert store.list_summaries() == summaries


def test_two_instances_preserve_independent_preferences(tmp_path):
    first, second = [LongTermMemory("alice", str(tmp_path)) for _ in range(2)]
    first.save_preference("hotel_brands", ["全季"])
    second.save_preference("seat", "二等座")
    assert first.get_preference() == {"hotel_brands": ["全季"], "seat": "二等座"}
    assert second.get_preference() == first.get_preference()


def test_user_tombstone_blocks_agent_restore(tmp_path):
    first, second = [LongTermMemory("alice", str(tmp_path)) for _ in range(2)]
    first.set_user_preference("hotel_brands", ["全季"])
    assert second.save_preference("hotel_brands", ["汉庭"]) is False
    first.delete_user_preference("hotel_brands")
    assert second.add_hotel_brand("如家") is False
    assert first.get_preference("hotel_brands") is None


def test_concurrent_list_preferences_merge(tmp_path):
    first, second = [LongTermMemory("alice", str(tmp_path)) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(
            pool.map(
                lambda pair: pair[0].add_hotel_brand(pair[1]), [(first, "全季"), (second, "汉庭")]
            )
        )
    assert set(first.get_preference("hotel_brands")) == {"全季", "汉庭"}


def test_clear_history_keeps_preferences_and_never_reimports(tmp_path):
    source = tmp_path / "alice.json"
    source.write_text(
        json.dumps(
            {
                "user_id": "alice",
                "preferences": {"seat": "二等座"},
                "trip_history": [{"destination": "上海"}],
                "chat_history": [],
            }
        ),
        encoding="utf-8",
    )
    original = source.read_bytes()
    memory = LongTermMemory("alice", str(tmp_path))
    assert len(memory.get_trip_history()) == 1
    memory.clear_history()
    reopened = LongTermMemory("alice", str(tmp_path))
    assert reopened.get_trip_history() == []
    assert reopened.get_preference("seat") == "二等座"
    reopened.delete_all()
    assert LongTermMemory("alice", str(tmp_path)).get_preference() == {}
    assert source.read_bytes() == original


def test_summary_select_does_not_load_full_content(tmp_path):
    store = TripStore(tmp_path, "alice")
    saved = store.save(populated_workflow(), expected_revision=None)
    assert hasattr(store, "storage"), "workflow storage must use a database"
    with store.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET content_json='invalid' WHERE id=?", (saved["id"],))
    assert store.list_summaries()[0]["id"] == saved["id"]
    with pytest.raises(ValueError):
        store.load(saved["id"])


def test_trip_export_failure_does_not_undo_commit(tmp_path, monkeypatch):
    store = TripStore(tmp_path, "alice")

    def fail(*args):
        raise OSError("export unavailable")

    monkeypatch.setattr(store.trip_memory, "rebuild", fail)
    saved = store.save(populated_workflow(), expected_revision=None)
    assert store.load(saved["id"]) == saved


def test_restore_does_not_decode_covered_or_tool_payloads(tmp_path):
    from trip_assistant.memory.context import build_context
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "alice", "s")
    for seq in range(2):
        store.append(
            {
                "type": "message",
                "scope": "session",
                "turn_id": "old",
                "role": "user" if seq == 0 else "assistant",
                "content": "old",
                "final": seq == 1,
            }
        )
    store.append({"type": "agent_stage", "scope": "policy", "content": {"audit": "private"}})
    store.update_state({"summary": "old summary", "covered_through_seq": 2})
    for n in range(24):
        for role in ("user", "assistant"):
            store.append(
                {
                    "type": "message",
                    "scope": "session",
                    "turn_id": str(n),
                    "role": role,
                    "content": role + str(n),
                    "final": role == "assistant",
                }
            )
    with store.storage.transaction() as conn:
        conn.execute(
            "UPDATE events SET payload_json='cannot decode' WHERE seq<=2 OR scope<>'session'"
        )
    snapshot = build_context(SessionStore(tmp_path, "alice", "s"))
    assert "old summary" in snapshot[0]["content"] and len(snapshot) == 49


def test_tool_checks_do_not_read_history(tmp_path, monkeypatch):
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "alice", "s")

    def fail(*args, **kwargs):
        raise AssertionError("full event read")

    monkeypatch.setattr(store, "read_events", fail)
    store.record_call({"id": "call", "name": "query", "arguments": {}}, "turn")
    assert store.pending_tool_calls()[0]["id"] == "call"
    store.record_result("call", {"status": "ok"}, "turn")
    assert not store.pending_tool_calls()


@pytest.mark.asyncio
async def test_async_summary_cannot_restore_cleared_context(tmp_path):
    from trip_assistant.memory.compaction import compact_completed_turns
    from trip_assistant.memory.context import build_context
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "alice", "s")
    for n in range(3):
        for role in ("user", "assistant"):
            store.append(
                {
                    "type": "message",
                    "scope": "session",
                    "turn_id": str(n),
                    "role": role,
                    "content": "x" * 40,
                    "final": role == "assistant",
                }
            )

    async def summarize(previous, events):
        store.clear_context()
        return "old facts"

    await compact_completed_turns(
        store, summarizer=summarize, request_fits=lambda summary, events: bool(summary)
    )
    state = store.read_state()
    assert state["summary"] == "" and state["cleared_through_seq"] == 6
    assert build_context(store) == []
