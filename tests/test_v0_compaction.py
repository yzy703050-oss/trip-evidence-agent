"""Compaction cuts only at recoverable conversation boundaries."""

import pytest

from context.compaction import select_safe_prefix
from context.memory_manager import MemoryManager


def _event(seq, turn, kind, content="x", **extra):
    return {
        "seq": seq,
        "turn_id": turn,
        "scope": extra.pop("scope", "session"),
        "type": kind,
        "content": content,
        **extra,
    }


def test_completed_turn_cut_keeps_recent_turn_intact():
    events = [
        _event(1, "t1", "message", "11111111", role="user"),
        _event(2, "t1", "message", "22222222", role="assistant", final=True),
        _event(3, "t2", "message", "33333333", role="user"),
        _event(4, "t2", "message", "44444444", role="assistant", final=True),
        _event(5, "t3", "message", "55555555", role="user"),
    ]

    assert (
        select_safe_prefix(
            events,
            token_budget=10,
            token_counter=lambda event: len(event["content"]),
            keep_recent_turns=1,
        )
        == 4
    )


def test_active_turn_cut_uses_completed_stage_and_retains_pending_call():
    events = [
        _event(1, "t1", "message", "用户的完整目标和约束", role="user"),
        _event(
            2,
            "t1",
            "tool_call",
            "",
            scope="agent:planner",
            tool_calls=[{"id": "c1", "name": "search"}],
        ),
        _event(3, "t1", "tool_result", "第一步结果", scope="agent:planner", tool_call_id="c1"),
        _event(4, "t1", "stage_complete", "第一阶段完成", scope="run"),
        _event(
            5,
            "t1",
            "tool_call",
            "待完成调用",
            scope="agent:planner",
            tool_calls=[{"id": "c2", "name": "search"}],
        ),
    ]

    cut = select_safe_prefix(
        events,
        token_budget=8,
        token_counter=lambda event: len(event["content"]),
        keep_recent_turns=1,
    )
    assert cut == 4
    assert [event["seq"] for event in events if event["seq"] > cut] == [5]


def test_pending_tool_call_cannot_be_compacted_without_safe_boundary():
    events = [
        _event(1, "t1", "message", "很长的问题和要求", role="user"),
        _event(
            2,
            "t1",
            "tool_call",
            "很长的工具参数",
            scope="agent:planner",
            tool_calls=[{"id": "c1", "name": "search"}],
        ),
    ]
    assert (
        select_safe_prefix(
            events, token_budget=2, token_counter=lambda event: len(event["content"])
        )
        is None
    )


def test_compaction_persists_checkpoint_and_preserves_original_events(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(3):
        turn = memory.start_turn(f"问题{index}" + "很长" * 10)
        memory.record_message("assistant", f"答案{index}" + "很长" * 10, turn, final=True)
    original = memory.session_store.read_events()

    def summarize(previous, selected):
        return "前两轮讨论了出行需求"

    first = memory.compact_if_needed(
        token_budget=30,
        summarizer=summarize,
        keep_recent_turns=1,
        token_counter=lambda event: len(str(event.get("content", ""))),
    )
    assert first["covered_through_seq"] == 4
    assert memory.session_store.read_events() == original

    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    context = reopened.get_compacted_context()
    assert context["summary"] == "前两轮讨论了出行需求"
    assert [item["content"] for item in context["recent_messages"]] == [
        "问题2" + "很长" * 10,
        "答案2" + "很长" * 10,
    ]
    assert (
        reopened.compact_if_needed(
            token_budget=30,
            summarizer=summarize,
            keep_recent_turns=1,
            token_counter=lambda event: len(str(event.get("content", ""))),
        )["covered_through_seq"]
        == 4
    )


@pytest.mark.asyncio
async def test_async_compaction_reads_streamed_summary_text(tmp_path):
    class StreamingModel:
        async def __call__(self, _messages):
            async def chunks():
                yield {"content": "旧"}
                yield {"content": "旧对话已概括"}

            return chunks()

    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path), llm_model=StreamingModel())
    for index in range(3):
        turn = memory.start_turn(f"问题{index}" + "长" * 20)
        memory.record_message("assistant", "回答" + "长" * 20, turn, final=True)
    state = await memory.compact_if_needed_async(
        20, keep_recent_turns=1, token_counter=lambda event: len(str(event.get("content", "")))
    )
    assert state["summary"] == "旧对话已概括"
