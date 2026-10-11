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
        )
        == 4
    )


def test_active_turn_stages_never_form_session_cut():
    events = [
        _event(-1, "old", "message", "旧" * 40, role="user"),
        _event(0, "old", "message", "旧答" * 20, role="assistant", final=True),
        _event(1, "t1", "message", "目标", role="user"),
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
        token_budget=30,
        token_counter=lambda event: len(event["content"]),
    )
    assert cut == 0
    assert [event["seq"] for event in events if event["seq"] > cut] == [1, 2, 3, 4, 5]


def test_unfinished_old_turn_blocks_prefix():
    events = [_event(1, "unfinished", "message", "old" * 40, role="user"),
              _event(2, "completed", "message", "q", role="user"),
              _event(3, "completed", "message", "a", role="assistant", final=True),
              _event(4, "active", "message", "q", role="user")]
    assert select_safe_prefix(events, 20, token_counter=lambda event: len(event["content"])) is None


def test_legacy_mid_turn_summary_keeps_uncovered_messages(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn = memory.start_turn("旧目标")
    memory.record_message("assistant", "未覆盖答复", turn, final=True)
    memory.session_store.write_state({"summary": "旧摘要", "covered_through_seq": 1,
        "checkpoint": {"goal": "旧目标", "active_turn_id": turn}})
    for index in range(12):
        turn = memory.start_turn(f"q{index}")
        memory.record_message("assistant", f"a{index}", turn, final=True)
    context = MemoryManager("alice", "s1", storage_path=str(tmp_path)).get_compacted_context()
    assert len(context["recent_messages"]) == 25
    assert context["recent_messages"][0]["content"] == "未覆盖答复"
    assert "旧目标" in context["summary"]


def test_summary_failure_keeps_coverage(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for i in range(3):
        turn = memory.start_turn("q" * 40)
        memory.record_message("assistant", "a" * 40, turn, final=True)
    original = memory.session_store.read_state()
    def fail(_previous, _events):
        raise RuntimeError("summary unavailable")
    state = memory.compact_if_needed(100, summarizer=fail, token_counter=lambda e: len(e["content"]))
    assert state == original
    assert memory.session_store.read_state() == original
    assert len(memory.get_compacted_context()["recent_messages"]) == 6


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
        token_budget=80,
        summarizer=summarize,
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
            token_budget=80,
            summarizer=summarize,
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
        70, token_counter=lambda event: len(str(event.get("content", "")))
    )
    assert state["summary"] == "旧对话已概括"
