from trip_assistant.storage.sessions import SessionStore


def seed(store, tid, text="问题", done=True):
    store.append(
        {"type": "message", "scope": "session", "turn_id": tid, "role": "user", "content": text}
    )
    store.record_calls(
        [
            {"id": tid + "a", "name": "read", "arguments": {}},
            {"id": tid + "b", "name": "read", "arguments": {}},
        ],
        tid,
        content="查看",
    )
    for identity in (tid + "a", tid + "b"):
        store.record_result(
            identity,
            {
                "status": "ok",
                "data": {"items": [{"id": str(i)} for i in range(30)], "raw": "secret audit"},
            },
            tid,
        )
    if done:
        store.append(
            {
                "type": "message",
                "scope": "session",
                "turn_id": tid,
                "role": "assistant",
                "final": True,
                "content": "答复",
            }
        )


def test_native_projection_keeps_tool_pair_and_bounds_data(tmp_path):
    from trip_assistant.memory.context import build_context

    store = SessionStore(tmp_path, "a", "s")
    seed(store, "t")
    store.append(
        {
            "scope": "agent:policy",
            "type": "message",
            "role": "assistant",
            "content": "child private",
        }
    )
    result = build_context(store)
    assert len(result) == 5 and len(result[1]["tool_calls"]) == 2
    assert "secret audit" not in str(result) and "child private" not in str(result)
    import json

    assert len(json.loads(result[2]["content"])["data"]["items"]) == 5


def test_boundary_only_uses_complete_old_turns_and_all_tool_results(tmp_path):
    from trip_assistant.memory.compaction import complete_prefix_boundaries

    store = SessionStore(tmp_path, "a", "s")
    seed(store, "old")
    seed(store, "current", done=False)
    events = store.read_events(scope="session")
    assert complete_prefix_boundaries(events) == [5]
    broken = [e for e in events if not (e.get("tool_call_id") == "oldb")]
    assert complete_prefix_boundaries(broken) == []
