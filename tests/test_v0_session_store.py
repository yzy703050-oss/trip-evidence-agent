"""V0 session persistence contracts; these tests never call a model."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from context.long_term_memory import LongTermMemory
from context.session_store import SessionStore


def test_interrupted_last_write_is_preserved_separately_and_next_event_remains_readable(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    store.append(
        {"turn_id": "t1", "type": "message", "scope": "session", "role": "user", "content": "完整"}
    )
    with store.events_path.open("ab") as handle:
        handle.write(b'{"turn_id":"interrupted"')

    assert [event["seq"] for event in SessionStore(tmp_path, "alice", "s1").read_events()] == [1]
    saved = store.append(
        {"turn_id": "t2", "type": "message", "scope": "session", "role": "user", "content": "继续"}
    )
    assert saved["seq"] == 2
    assert [event["seq"] for event in store.read_events()] == [1, 2]
    assert b"interrupted" in store.events_path.with_suffix(".jsonl.corrupt").read_bytes()


def test_session_events_survive_restart_in_append_order(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    first = store.append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "去北京",
        }
    )
    second = store.append(
        {
            "turn_id": "t1",
            "scope": "agent:planner",
            "type": "tool_call",
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "name": "search", "arguments": {}}],
        }
    )
    third = store.append(
        {
            "turn_id": "t1",
            "scope": "agent:planner",
            "type": "tool_result",
            "tool_call_id": "call-1",
            "content": {"price": 500},
            "status": "success",
        }
    )

    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
    restored = SessionStore(tmp_path, "alice", "s1").read_events()
    assert [event["type"] for event in restored] == ["message", "tool_call", "tool_result"]
    assert restored[2]["tool_call_id"] == restored[1]["tool_calls"][0]["id"]
    assert SessionStore(tmp_path, "alice", "other").read_events() == []


def test_parallel_appends_receive_unique_monotonic_sequence(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: store.append(
                    {
                        "turn_id": "t1",
                        "scope": "agent:a",
                        "type": "message",
                        "role": "assistant",
                        "content": str(index),
                    }
                ),
                range(40),
            )
        )

    assert [event["seq"] for event in store.read_events()] == list(range(1, 41))


def test_path_components_cannot_escape_storage_root(tmp_path):
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "..", "s1")
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "alice", "../other")


def test_legacy_user_json_migrates_once_without_deleting_source(tmp_path):
    legacy = {
        "user_id": "alice",
        "preferences": [{"type": "hotel_brands", "value": ["全季"]}],
        "trip_history": [{"trip_id": "trip_1", "destination": "北京"}],
        "chat_history": [
            {
                "role": "user",
                "content": "去北京",
                "timestamp": "2026-01-01T10:00:00",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": "好的",
                "timestamp": "2026-01-01T10:00:01",
                "session_id": "s1",
            },
            {
                "role": "user",
                "content": "去上海",
                "timestamp": "2026-01-02T10:00:00",
                "session_id": "s2",
            },
        ],
        "statistics": {
            "total_messages": 3,
            "total_trips": 1,
            "total_queries": 2,
            "frequent_destinations": {"北京": 1},
        },
    }
    old_path = tmp_path / "alice.json"
    old_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    memory = LongTermMemory("alice", str(tmp_path))
    assert memory.get_preference("hotel_brands") == ["全季"]
    assert memory.get_trip_history()[0]["destination"] == "北京"
    assert [item["content"] for item in memory.get_chat_history(session_id="s1")] == [
        "去北京",
        "好的",
    ]
    assert (tmp_path / "alice" / "profile.json").exists()
    assert (tmp_path / "alice" / "trips.json").exists()
    assert (tmp_path / "alice" / "sessions" / "s1" / "events.jsonl").exists()
    assert json.loads(old_path.read_text(encoding="utf-8")) == legacy

    reopened = LongTermMemory("alice", str(tmp_path))
    assert len(reopened.get_chat_history(session_id="s1")) == 2
    assert len(reopened.get_chat_history(session_id="s2")) == 1
