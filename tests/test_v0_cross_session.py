"""Cross-session recall uses stored, scoped summaries rather than full history."""

import pytest

from context.memory_manager import MemoryManager


@pytest.mark.asyncio
async def test_cross_session_summary_is_cached_and_refreshed_only_after_new_messages(tmp_path):
    previous = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    first = previous.start_turn("我去北京，住宿不超过500元")
    previous.record_message("assistant", "已记录北京住宿预算", first, final=True)

    class Summarizer:
        def __init__(self):
            self.calls = 0

        async def __call__(self, _messages):
            self.calls += 1

            class Response:
                content = "北京住宿预算不超过500元"

            return Response()

    model = Summarizer()
    current = MemoryManager("alice", "s2", storage_path=str(tmp_path), llm_model=model)
    assert "北京住宿预算不超过500元" in await current.get_long_term_summary_async(query="北京")
    assert "北京住宿预算不超过500元" in await current.get_long_term_summary_async(query="北京")
    assert model.calls == 1

    second = previous.start_turn("改成不超过600元")
    previous.record_message("assistant", "已更新", second, final=True)
    await current.get_long_term_summary_async(query="北京")
    assert model.calls == 2


@pytest.mark.asyncio
async def test_cross_session_selection_excludes_unrelated_internal_tool_messages(tmp_path):
    beijing = MemoryManager("alice", "beijing", storage_path=str(tmp_path))
    turn = beijing.start_turn("北京差旅政策")
    beijing.record_message("assistant", "北京住宿上限500元", turn, final=True)
    beijing.record_message("assistant", "内部调试密钥", turn, scope="agent:rag")

    shanghai = MemoryManager("alice", "shanghai", storage_path=str(tmp_path))
    turn = shanghai.start_turn("上海交通政策")
    shanghai.record_message("assistant", "上海优先高铁", turn, final=True)

    current = MemoryManager("alice", "current", storage_path=str(tmp_path))
    result = await current.get_long_term_summary_async(query="上海")
    assert "上海优先高铁" in result
    assert "北京住宿" not in result
    assert "内部调试密钥" not in result


@pytest.mark.asyncio
async def test_cross_session_query_does_not_inject_unrelated_latest_session(tmp_path):
    previous = MemoryManager("alice", "hotel", storage_path=str(tmp_path))
    turn = previous.start_turn("北京住宿标准")
    previous.record_message("assistant", "北京住宿上限500元", turn, final=True)

    current = MemoryManager("alice", "current", storage_path=str(tmp_path))
    assert await current.get_long_term_summary_async(query="广州航班") == ""
