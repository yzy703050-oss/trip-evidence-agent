import pytest

from context.memory_manager import MemoryManager


def test_conditional_text_preserved_and_incremental(tmp_path):
    from agents.memory_tools import apply_preference_updates, parse_memory_command, read_memory

    memory = MemoryManager("alice", "s", str(tmp_path))
    command = parse_memory_command("以后出差都尽量给我选择高铁")
    first = apply_preference_updates(memory, command["updates"])
    assert first["effective_memories"][0]["text"] == "以后出差都尽量给我选择高铁"
    apply_preference_updates(memory, [{"op": "add", "text": "带孩子旅行时优先安静酒店"}])
    assert len(read_memory(memory, {})["memories"]) == 2
    restored = MemoryManager("alice", "new", str(tmp_path))
    assert len(read_memory(restored, {})["memories"]) == 2
    assert read_memory(MemoryManager("bob", "s", str(tmp_path)), {})["memories"] == []


def test_targeted_replace_retract_and_protection(tmp_path):
    from agents.memory_tools import apply_preference_updates, read_memory

    memory = MemoryManager("alice", "s", str(tmp_path))
    first = apply_preference_updates(memory, [{"op": "add", "text": "出差优先高铁"}])
    identity = first["effective_memories"][0]["id"]
    apply_preference_updates(memory, [{"op": "replace", "id": identity, "text": "出差优先卧铺"}])
    apply_preference_updates(memory, [{"op": "retract", "id": identity}])
    rejected = apply_preference_updates(
        memory, [{"op": "replace", "id": identity, "text": "出差优先高铁", "source": "history"}]
    )
    assert rejected["rejected"]
    assert read_memory(memory, {})["memories"] == []
    assert memory.long_term.data["travel_memories"][0]["status"] == "retracted"


def test_temporary_ambiguous_and_mixed_parse():
    from agents.memory_tools import parse_memory_command

    assert parse_memory_command("这次预算500元，周五出发") is None
    assert parse_memory_command("修改我的偏好")["missing_fields"] == ["memory_id", "text"]
    parsed = parse_memory_command("以后出差尽量高铁，帮我规划明天去上海")
    assert parsed["updates"][0]["text"] == "以后出差尽量高铁"
    assert parsed["remaining_query"] == "帮我规划明天去上海"


def test_old_keys_remain_readable_and_failed_write_not_confirmed(tmp_path, monkeypatch):
    from agents.memory_tools import apply_preference_updates, read_memory

    memory = MemoryManager("alice", "s", str(tmp_path))
    memory.long_term.set_user_preference("hotel_brands", ["汉庭"])
    assert read_memory(memory, {})["legacy_preferences"] == {"hotel_brands": ["汉庭"]}

    def fail(*args):
        raise OSError("save failed")

    monkeypatch.setattr(memory.long_term, "apply_memory_updates", fail)
    result = apply_preference_updates(memory, [{"op": "add", "text": "以后高铁"}])
    assert result["status"] == "error" and not result["applied"]


@pytest.mark.asyncio
async def test_preference_only_updates_and_reads_without_model(tmp_path):
    from agents.contracts import RunState
    from agents.execution_harness import ExecutionHarness

    class Never:
        async def initialize(self, context):
            pytest.fail("pure memory must not call model")

    memory = MemoryManager("alice", "s", str(tmp_path))
    harness = ExecutionHarness(main_agent=Never(), agent_registry={}, memory_manager=memory)
    result = await harness.run_turn({"original_query": "以后出差都尽量给我选择高铁"}, RunState("t"))
    assert result["status"] == "ok" and "高铁" in result["final_answer"]
    result = await harness.run_turn({"original_query": "查看我的偏好"}, RunState("t2"))
    assert "高铁" in result["final_answer"]
