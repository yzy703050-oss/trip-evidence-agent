from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json

import pytest

from context.long_term_memory import LongTermMemory
from context.workflow_store import WorkflowStore
from workflow_support import populated_workflow


def test_workflow_summary_failure_preserves_previous_revision(tmp_path, monkeypatch):
    import context.workflow_index as module

    store = WorkflowStore(tmp_path, "alice")
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
    store = WorkflowStore(tmp_path, "alice")
    saved = store.save(populated_workflow(), expected_revision=None)
    assert hasattr(store, "storage"), "workflow storage must use a database"
    with store.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET content_json='invalid' WHERE id=?", (saved["id"],))
    assert store.list_summaries()[0]["id"] == saved["id"]
    with pytest.raises(ValueError):
        store.load(saved["id"])


def test_trip_export_failure_does_not_undo_commit(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path, "alice")

    def fail(*args):
        raise OSError("export unavailable")

    monkeypatch.setattr(store.trip_memory, "rebuild", fail)
    saved = store.save(populated_workflow(), expected_revision=None)
    assert store.load(saved["id"]) == saved


def test_restore_does_not_decode_covered_or_tool_payloads(tmp_path):
    from context.memory_manager import MemoryManager

    memory = MemoryManager("alice", "s", storage_path=str(tmp_path))
    turn = memory.start_turn("old")
    memory.record_message("assistant", "old reply", turn, final=True)
    memory.record_agent_stage("information_query", 1, {"summary": "audit"}, turn)
    memory.session_store.update_state({"summary": "old summary", "covered_through_seq": 2})
    for n in range(24):
        turn = memory.start_turn(f"new {n}")
        memory.record_message("assistant", f"reply {n}", turn, final=True)
    with memory.session_store.storage.transaction() as conn:
        conn.execute(
            "UPDATE events SET payload_json='cannot decode' WHERE seq<=2 OR scope<>'session'"
        )
    restarted = MemoryManager("alice", "s", storage_path=str(tmp_path))
    snapshot = restarted.get_compacted_context()
    assert snapshot["summary"] == "old summary"
    assert len(snapshot["recent_messages"]) == 48
    assert len(restarted.short_term.messages) == 48


def test_tool_checks_do_not_read_history(tmp_path, monkeypatch):
    from context.memory_manager import MemoryManager

    memory = MemoryManager("alice", "s", storage_path=str(tmp_path))
    turn = memory.start_turn("q")

    def fail(*args, **kwargs):
        raise AssertionError("full event read")

    monkeypatch.setattr(memory.session_store, "read_events", fail)
    memory.record_tool_call([{"id": "call", "name": "weather"}], turn, "info")
    assert memory.get_pending_tool_calls()[0]["id"] == "call"
    memory.record_tool_result("call", "sunny", turn, "info")
    assert memory.get_pending_tool_calls() == []
    assert len(memory.short_term.messages) == 1


@pytest.mark.asyncio
async def test_async_summary_cannot_restore_cleared_context(tmp_path):
    from types import SimpleNamespace
    from context.memory_manager import MemoryManager

    memory = MemoryManager("alice", "s", storage_path=str(tmp_path))
    for _ in range(3):
        turn = memory.start_turn("q" * 40)
        memory.record_message("assistant", "a" * 40, turn, final=True)

    async def summarize(messages):
        memory.clear_short_term()
        return SimpleNamespace(text="old facts")

    memory.llm_model = summarize
    result = await memory.compact_if_needed_async(
        100, token_counter=lambda row: len(row.get("content", ""))
    )
    state = memory.session_store.read_state()
    assert result == state
    assert state["summary"] == ""
    assert state["cleared_through_seq"] == 6
    assert memory.get_compacted_context()["recent_messages"] == []
