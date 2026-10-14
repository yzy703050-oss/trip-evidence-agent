import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.context_projection import project_model_context
from agents.main_agent import MainAgent
from context.memory_manager import MemoryManager


def history():
    return [
        {
            "seq": 2,
            "turn_id": "old",
            "scope": "session",
            "type": "message",
            "role": "assistant",
            "content": "日期更正为10月20日，房价未知",
            "final": True,
            "timestamp": "private-time",
            "result_summary": {"workflow_id": "w1", "workflow_revision": 3},
        }
    ]


def test_explicit_history_uses_business_view_without_mutating_events():
    context = {"recent_messages": history()}
    original = deepcopy(context)
    view = project_model_context(context)
    assert set(view["recent_messages"][0]) == {"role", "content", "result_summary"}
    assert view["recent_messages"][0]["result_summary"]["workflow_revision"] == 3
    assert context == original


def test_refresh_and_restart_preserve_facts_but_remove_management_fields(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    turn = memory.start_turn("日期改为10月20日")
    memory.record_message(
        "assistant",
        json.dumps({"final_answer": "房价未知", "workflow_id": "w1", "workflow_revision": 3}),
        turn,
        final=True,
        metadata={"format": "main_result"},
    )
    before = memory.session_store.read_events()
    reopened = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    reopened.start_turn("回忆上次日期")
    view = MainAgent(None, memory_manager=reopened)._refresh_history({})
    assert all(set(m) <= {"role", "content", "result_summary"} for m in view["recent_messages"])
    assert view["recent_messages"][-1]["result_summary"]["workflow_id"] == "w1"
    assert memory.session_store.read_events()[: len(before)] == before


def test_summary_input_uses_same_business_view(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for _ in range(3):
        turn = memory.start_turn("更正日期，未知房价" * 30)
        memory.record_message("assistant", "已记住" * 30, turn, final=True)
    seen = []
    memory.compact_if_needed(
        200,
        summarizer=lambda old, events: seen.extend(events) or "简短摘要",
        token_counter=lambda e: len(e.get("content", "")),
    )
    assert seen
    assert all(set(m) <= {"role", "content", "result_summary"} for m in seen)


@pytest.mark.asyncio
async def test_current_request_remains_explicit_after_history_for_restart():
    async def model(messages, **kwargs):
        # A long history ends with an old amendment. The current request must
        # remain a distinct authoritative request after that background.
        if "仅回忆" not in messages[-1]["content"] or "旧修改" in messages[-1]["content"]:
            return SimpleNamespace(text='{"response_mode":"workflow","resume_workflow_id":"old"}')
        return SimpleNamespace(text='{"response_mode":"direct","final_answer":"3人，4晚"}')

    result = await MainAgent(model).initialize(
        {
            "original_query": "仅回忆当前人数和晚数",
            "recent_messages": [{"role": "user", "content": "旧修改：换酒店"}],
        }
    )
    assert result["response_mode"] == "direct"
