"""Session context must be recoverable without leaking child-agent internals."""

from agentscope.message import Msg

from context.memory_manager import MemoryManager


def test_session_resume_keeps_complete_turns_and_isolates_other_sessions(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    first = memory.start_turn("第一次问题")
    memory.record_message("assistant", "第一次回答", first, final=True)
    second = memory.start_turn("第二次问题")
    memory.record_message("assistant", "第二次回答", second, final=True)

    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    assert [
        (item["role"], item["content"]) for item in reopened.short_term.get_recent_context(1)
    ] == [("user", "第二次问题"), ("assistant", "第二次回答")]
    assert (
        MemoryManager("alice", "s2", storage_path=str(tmp_path)).short_term.get_recent_context()
        == []
    )


def test_tool_events_are_stored_but_do_not_leak_into_intent_context(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn_id = memory.start_turn("查价格")
    memory.record_msg(
        Msg(
            name="planner",
            role="assistant",
            content=[
                {"type": "tool_use", "id": "c1", "name": "search_price", "input": {"city": "北京"}}
            ],
        ),
        turn_id,
        scope="agent:planner",
    )
    memory.record_msg(
        Msg(
            name="system",
            role="system",
            content=[
                {
                    "type": "tool_result",
                    "id": "c1",
                    "name": "search_price",
                    "output": [{"type": "text", "text": "500元"}],
                }
            ],
        ),
        turn_id,
        scope="agent:planner",
    )
    memory.record_message("assistant", "价格约500元", turn_id, final=True)

    assert [item["type"] for item in memory.get_agent_events("planner")] == [
        "tool_call",
        "tool_result",
    ]
    assert [item["content"] for item in memory.short_term.get_recent_context(1)] == [
        "查价格",
        "价格约500元",
    ]
    assert memory.get_pending_tool_calls() == []


def test_pending_tool_call_survives_restart_and_result_requires_matching_id(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn_id = memory.start_turn("查价格")
    memory.record_tool_call(
        [{"id": "c1", "name": "search_price", "arguments": {}}], turn_id, "agent:planner"
    )

    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    assert [item["id"] for item in reopened.get_pending_tool_calls()] == ["c1"]
    try:
        reopened.record_tool_result("unknown", "x", turn_id, "agent:planner")
    except ValueError:
        pass
    else:
        raise AssertionError("orphan tool results must be rejected")
    reopened.record_tool_result("c1", "暂时无法获取价格", turn_id, "agent:planner", status="error")
    assert reopened.get_pending_tool_calls() == []


def test_clear_context_does_not_delete_raw_log_and_persists_across_restart(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn_id = memory.start_turn("旧问题")
    memory.record_message("assistant", "旧回答", turn_id, final=True)
    memory.clear_short_term()

    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    assert reopened.short_term.get_recent_context() == []
    assert len(reopened.session_store.read_events()) == 2


def test_clear_context_also_drops_old_compaction_summary(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(3):
        turn = memory.start_turn(f"旧问题{index}" + "长" * 20)
        memory.record_message("assistant", "旧回答" + "长" * 20, turn, final=True)
    memory.compact_if_needed(
        10,
        summarizer=lambda _old, _events: "旧会话摘要",
        keep_recent_turns=1,
        token_counter=lambda event: len(str(event.get("content", ""))),
    )
    assert memory.get_compacted_context()["summary"]

    memory.clear_short_term()
    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    assert reopened.get_compacted_context()["summary"] == ""
    assert reopened.get_compacted_context()["recent_messages"] == []
    assert len(reopened.session_store.read_events()) == 6


def test_clear_abandons_pending_tool_calls_from_previous_task(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn_id = memory.start_turn("旧任务")
    memory.record_tool_call(
        [{"id": "c1", "name": "search", "arguments": {}}], turn_id, "agent:planner"
    )
    assert [call["id"] for call in memory.get_pending_tool_calls()] == ["c1"]

    memory.clear_short_term()
    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    assert reopened.get_pending_tool_calls() == []
