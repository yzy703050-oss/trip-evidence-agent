import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from workflow_support import populated_workflow

from context.long_term_memory import LongTermMemory
from context.memory_manager import MemoryManager


def completed(memory):
    workflow = populated_workflow()
    workflow["status"] = "completed"
    return memory.workflow_store.save(workflow, expected_revision=None)


def test_new_user_and_preference_save_do_not_create_trips_file(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.set_user_preference("home_location", "上海")
    assert not (tmp_path / "alice" / "trips.json").exists()


def test_completed_workflow_is_history_without_a_second_save(tmp_path):
    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    row = memory.long_term.get_trip_history()[0]
    assert row["workflow_id"] == workflow["id"]
    assert row["destination"] == workflow["tasks"][-1]["destination"]
    assert row["record_kind"] == "planned_itinerary"
    assert memory.long_term.get_statistics()["total_trips"] == 1
    assert not MemoryManager("bob", "one", storage_path=str(tmp_path)).long_term.get_trip_history()


def test_latest_completed_revision_controls_history_and_statistics(tmp_path):
    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    workflow["tasks"][-1]["destination"] = "苏州"
    newer = memory.workflow_store.save(workflow, expected_revision=workflow["revision"])
    rows = LongTermMemory("alice", str(tmp_path)).get_trip_history()
    assert len(rows) == 1 and rows[0]["workflow_revision"] == newer["revision"]
    assert rows[0]["destination"] == "苏州"
    assert memory.long_term.get_frequent_destinations() == [("苏州", 1)]
    newer["status"] = "partial"
    memory.workflow_store.save(newer, expected_revision=newer["revision"])
    assert memory.long_term.get_trip_history() == []
    assert memory.get_active_workflows()[0]["id"] == newer["id"]


def legacy_source(tmp_path, rows):
    path = tmp_path / "alice" / "trips.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"trip_history": rows}, ensure_ascii=False), encoding="utf-8")
    return path


def test_legacy_import_is_idempotent_readonly_and_not_resumable(tmp_path):
    rows = [{"trip_id": "../unsafe", "destination": "上海", "summary": "旧方案"}]
    source = legacy_source(tmp_path, rows)
    before = source.read_bytes()
    for _ in range(2):
        memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
        assert memory.long_term.get_trip_history() == rows
        assert memory.get_known_workflows() == []
        assert memory.get_active_workflows() == []
    assert list((tmp_path / "alice" / "workflows").glob("history_*.json"))
    assert source.read_bytes() == before


def test_existing_workflow_wins_over_old_history_copy(tmp_path):
    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    legacy_source(tmp_path, [{"workflow_id": workflow["id"], "destination": "旧城市"}])
    reopened = MemoryManager("alice", "two", storage_path=str(tmp_path))
    assert len(reopened.long_term.get_trip_history()) == 1
    assert reopened.long_term.get_trip_history()[0]["destination"] != "旧城市"
    assert reopened.workflow_store.load(workflow["id"]) == workflow


def test_import_failure_retries_without_duplicates(tmp_path, monkeypatch):
    import context.workflow_store as module

    rows = [{"trip_id": "one", "destination": "上海"}, {"trip_id": "two", "destination": "北京"}]
    source = legacy_source(tmp_path, rows)
    original = module.atomic_json_write
    writes = 0

    def fail_second(path, value):
        nonlocal writes
        if path.name.startswith("history_"):
            writes += 1
            if writes == 2:
                raise OSError("interrupted migration")
        original(path, value)

    monkeypatch.setattr(module, "atomic_json_write", fail_second)
    with pytest.raises(OSError, match="interrupted migration"):
        LongTermMemory("alice", str(tmp_path))
    monkeypatch.setattr(module, "atomic_json_write", original)
    memory = LongTermMemory("alice", str(tmp_path))
    assert sorted(r["trip_id"] for r in memory.get_trip_history()) == ["one", "two"]
    assert json.loads(source.read_text(encoding="utf-8"))["trip_history"] == rows


def test_parallel_imports_and_save_keep_unique_history(tmp_path):
    legacy_source(tmp_path, [{"trip_id": "one", "destination": "上海"}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        histories = list(
            pool.map(lambda _: LongTermMemory("alice", str(tmp_path)).get_trip_history(), range(2))
        )
    assert all(len(rows) == 1 for rows in histories)
    assert len(list((tmp_path / "alice" / "workflows").glob("history_*.json"))) == 1


def test_identical_legacy_records_without_ids_are_not_lost(tmp_path):
    rows = [{"destination": "上海"}, {"destination": "上海"}]
    legacy_source(tmp_path, rows)
    for _ in range(2):
        assert LongTermMemory("alice", str(tmp_path)).get_trip_history() == rows


def test_split_history_takes_precedence_over_obsolete_user_backup(tmp_path):
    legacy_source(tmp_path, [{"trip_id": "one", "destination": "新城市"}])
    (tmp_path / "alice.json").write_text(
        json.dumps(
            {
                "trip_history": [
                    {"trip_id": "one", "destination": "旧城市"},
                    {"trip_id": "removed", "destination": "已删除"},
                ]
            }
        ),
        encoding="utf-8",
    )
    rows = LongTermMemory("alice", str(tmp_path)).get_trip_history()
    assert len(rows) == 1 and rows[0]["destination"] == "新城市"


def test_same_trip_and_workflow_id_do_not_collide(tmp_path):
    legacy_source(
        tmp_path,
        [{"trip_id": "one", "destination": "上海"}, {"workflow_id": "one", "destination": "北京"}],
    )
    assert len(LongTermMemory("alice", str(tmp_path)).get_trip_history()) == 2


def test_duplicate_legacy_workflow_keeps_latest_revision(tmp_path):
    legacy_source(
        tmp_path,
        [
            {"workflow_id": "old", "workflow_revision": 1, "destination": "旧城市"},
            {"workflow_id": "old", "workflow_revision": 3, "destination": "新城市"},
            {"workflow_id": "old", "workflow_revision": 2, "destination": "中间城市"},
        ],
    )
    for _ in range(2):
        rows = LongTermMemory("alice", str(tmp_path)).get_trip_history()
        assert len(rows) == 1 and rows[0]["workflow_revision"] == 3


def test_updating_generated_history_id_keeps_one_record_and_existing_fields(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.save_trip_history({"destination": "上海", "summary": "原方案"})
    row = memory.get_trip_history()[0]
    memory.save_trip_history({"trip_id": row["trip_id"], "summary": "修改后方案"})
    rows = memory.get_trip_history()
    assert len(rows) == 1
    assert rows[0]["destination"] == "上海" and rows[0]["summary"] == "修改后方案"


def test_clear_does_not_revive_backup_or_remove_workflow(tmp_path):
    source = legacy_source(tmp_path, [{"destination": "旧城市"}])
    before = source.read_bytes()
    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    memory.long_term.set_user_preference("home_location", "杭州")
    memory.long_term.clear_history()
    reopened = MemoryManager("alice", "two", storage_path=str(tmp_path))
    assert reopened.long_term.get_trip_history() == []
    assert reopened.workflow_store.load(workflow["id"]) == workflow
    assert reopened.long_term.get_preference("home_location") == "杭州"
    assert source.read_bytes() == before
    reopened.workflow_store.save(workflow, expected_revision=workflow["revision"])
    assert len(reopened.long_term.get_trip_history()) == 1


def test_compatibility_history_saves_use_unified_storage_and_limit(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.save_trip_history({"destination": "上海"})
    memory.save_trip_history({"destination": "北京"})
    assert [r["destination"] for r in memory.get_trip_history(1)] == ["北京"]
    assert len(memory.get_trip_history(None)) == 2
    assert memory.get_statistics()["total_trips"] == 2
    assert not (tmp_path / "alice" / "trips.json").exists()
    assert len(list((tmp_path / "alice" / "workflows").glob("history_*.json"))) == 2


@pytest.mark.asyncio
async def test_real_workflow_completion_never_writes_history_copy(tmp_path, monkeypatch):
    from workflow_runtime import Runtime

    runtime = Runtime(tmp_path)

    def duplicate_save(*args):
        raise AssertionError("workflow history should be derived from canonical state")

    monkeypatch.setattr(runtime.memory.long_term, "save_trip_history", duplicate_save)
    result = await runtime.turn()
    assert result["status"] == "completed"
    row = runtime.memory.long_term.get_trip_history()[0]
    assert row["itinerary"] == result["workflow"]["validation"]
    assert row["workflow_revision"] == result["workflow_revision"]
    assert not list((tmp_path / "alice" / "workflows").glob("history_*.json"))


def test_summary_reads_migrated_history_when_backup_is_unavailable(tmp_path):
    source = legacy_source(tmp_path, [{"trip_id": "one", "summary": "旧旅行概要"}])
    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    # Removing only this test fixture proves the migrated records are self-contained.
    source.unlink()
    memory.get_known_workflows()
    assert "旧旅行概要" in (tmp_path / "alice" / "trip.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cli_and_memory_agent_use_completed_workflow_history(tmp_path):
    import importlib.util
    import io
    from pathlib import Path
    from types import SimpleNamespace

    from agentscope.message import Msg
    from rich.console import Console

    from cli import TripEvidenceCLI

    memory = MemoryManager("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    city = workflow["tasks"][-1]["destination"]
    app = TripEvidenceCLI()
    app.memory_manager = memory
    output = io.StringIO()
    app.console = Console(file=output, width=200, color_system=None)
    app.show_history()
    assert city in output.getvalue()
    assert city in await app._get_long_term_summary(city)

    spec = importlib.util.spec_from_file_location(
        "unified_memory_agent", Path(".claude/skills/memory-query/script/agent.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompts = []

    async def model(messages, **kwargs):
        prompts.append(str(messages))
        return SimpleNamespace(content=[{"type": "text", "text": "已找到保存的规划"}])

    agent = module.MemoryQueryAgent(model=model, memory_manager=memory)
    response = await agent.reply(
        Msg("user", json.dumps({"context": {"rewritten_query": "查之前的方案"}}), "user")
    )
    assert city in prompts[0]
    assert json.loads(response.content)["memory_sources"]["trip_count"] == 1
