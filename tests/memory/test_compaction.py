import pytest

from trip_assistant.storage.sessions import SessionStore


@pytest.mark.asyncio
async def test_compaction_commits_complete_prefix_without_deleting_audit(tmp_path):
    from trip_assistant.memory.compaction import compact_completed_turns

    store = SessionStore(tmp_path, "a", "s")
    for tid in ("old", "current"):
        store.append(
            {
                "scope": "session",
                "type": "message",
                "turn_id": tid,
                "role": "user",
                "content": "问题" * 100,
            }
        )
        if tid == "old":
            store.append(
                {
                    "scope": "session",
                    "type": "message",
                    "turn_id": tid,
                    "role": "assistant",
                    "final": True,
                    "content": "答复" * 100,
                }
            )
    raw = store.read_events()

    async def summary(previous, events):
        return "保留旧目标与事实。"

    result = await compact_completed_turns(
        store, summarizer=summary, request_fits=lambda summary, events: bool(summary)
    )
    assert result["covered_through_seq"] == 2 and store.read_events() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "error", "concurrent_clear"])
async def test_summary_failure_or_concurrent_clear_does_not_advance_coverage(tmp_path, mode):
    from trip_assistant.memory.compaction import compact_completed_turns

    store = SessionStore(tmp_path, "a", "s")
    for tid in ("old", "current"):
        store.append(
            {
                "scope": "session",
                "type": "message",
                "turn_id": tid,
                "role": "user",
                "content": "问题",
            }
        )
        if tid == "old":
            store.append(
                {
                    "scope": "session",
                    "type": "message",
                    "turn_id": tid,
                    "role": "assistant",
                    "final": True,
                    "content": "答复",
                }
            )

    async def summary(previous, events):
        if mode == "error":
            raise RuntimeError("failed")
        if mode == "concurrent_clear":
            store.clear_context()
            return "stale"
        return ""

    result = await compact_completed_turns(
        store, summarizer=summary, request_fits=lambda summary, events: bool(summary)
    )
    assert not result.get("summary")
    assert result.get("covered_through_seq", 0) == (3 if mode == "concurrent_clear" else 0)
