import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from support.native import active_trips, make_application
from support.workflow import populated_workflow

from trip_assistant.storage.identity import resolve_user_directory
from trip_assistant.storage.preferences import LongTermMemory


def completed(memory):
    workflow = populated_workflow()
    workflow["status"] = "completed"
    return memory.trips.save(workflow, expected_revision=None)


def test_new_user_and_preference_save_do_not_create_trips_file(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.set_user_preference("home_location", "上海")
    assert not (resolve_user_directory(tmp_path, "alice") / "trips.json").exists()


def test_completed_workflow_is_history_without_a_second_save(tmp_path):
    memory = make_application("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    row = memory.preferences.get_trip_history()[0]
    assert row["workflow_id"] == workflow["id"]
    assert row["destination"] == workflow["tasks"][-1]["destination"]
    assert row["record_kind"] == "planned_itinerary"
    assert memory.preferences.get_statistics()["total_trips"] == 1
    assert not make_application(
        "bob", "one", storage_path=str(tmp_path)
    ).preferences.get_trip_history()


def test_latest_completed_revision_controls_history_and_statistics(tmp_path):
    memory = make_application("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    workflow["tasks"][-1]["destination"] = "苏州"
    # A completed revision must carry evidence for the new destination.
    from support.workflow_queries import put_train

    query = put_train(workflow, len(workflow["tasks"]) - 1)
    workflow["tasks"][-1]["draft_plan"]["train_selection"] = {
        "query_id": query["id"],
        "result_revision": query["result_revision"],
        "candidate_id": query["items"][0]["id"],
    }
    newer = memory.trips.save(workflow, expected_revision=workflow["revision"])
    rows = LongTermMemory("alice", str(tmp_path)).get_trip_history()
    assert len(rows) == 1 and rows[0]["workflow_revision"] == newer["revision"]
    assert rows[0]["destination"] == "苏州"
    assert memory.preferences.get_frequent_destinations() == [("苏州", 1)]
    newer["status"] = "partial"
    memory.trips.save(newer, expected_revision=newer["revision"])
    memory.sessions.add_active_workflow(newer["id"])
    assert memory.preferences.get_trip_history() == []
    assert active_trips(memory)[0]["id"] == newer["id"]


def legacy_source(tmp_path, rows):
    path = tmp_path / "alice" / "trips.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"user_id": "alice", "trip_history": rows}, ensure_ascii=False), encoding="utf-8"
    )
    return path


def test_legacy_import_is_idempotent_readonly_and_not_resumable(tmp_path):
    rows = [{"trip_id": "../unsafe", "destination": "上海", "summary": "旧方案"}]
    source = legacy_source(tmp_path, rows)
    before = source.read_bytes()
    for _ in range(2):
        memory = make_application("alice", "one", storage_path=str(tmp_path))
        assert memory.preferences.get_trip_history() == rows
        assert memory.trips.list_summaries() == []
        assert active_trips(memory) == []
    assert len(memory.preferences.get_trip_history(None)) == 1
    assert memory.preferences.db_path.exists()
    assert source.read_bytes() == before


def test_existing_workflow_wins_over_old_history_copy(tmp_path):
    memory = make_application("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    legacy_source(tmp_path, [{"workflow_id": workflow["id"], "destination": "旧城市"}])
    reopened = make_application("alice", "two", storage_path=str(tmp_path))
    assert len(reopened.preferences.get_trip_history()) == 1
    assert reopened.preferences.get_trip_history()[0]["destination"] != "旧城市"
    assert reopened.trips.load(workflow["id"]) == workflow


def test_import_failure_retries_without_duplicates(tmp_path):
    import sqlite3

    from trip_assistant.storage.sqlite import SQLiteStorage, metadata

    rows = [{"trip_id": "one", "destination": "上海"}, {"trip_id": "two", "destination": "北京"}]
    source = legacy_source(tmp_path, rows)
    before = source.read_bytes()
    db = SQLiteStorage(tmp_path, "alice")
    with db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail BEFORE INSERT ON travel_history "
            "WHEN (SELECT COUNT(*) FROM travel_history)=1 "
            "BEGIN SELECT RAISE(ABORT,'interrupted migration'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="interrupted migration"):
        LongTermMemory("alice", str(tmp_path))
    with db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM travel_history").fetchone()[0] == 0
        assert metadata(conn, "migration_complete") is None
        conn.execute("DROP TRIGGER fail")
    memory = LongTermMemory("alice", str(tmp_path))
    assert sorted(r["trip_id"] for r in memory.get_trip_history()) == ["one", "two"]
    assert source.read_bytes() == before


def test_parallel_imports_and_save_keep_unique_history(tmp_path):
    legacy_source(tmp_path, [{"trip_id": "one", "destination": "上海"}])
    with ThreadPoolExecutor(max_workers=2) as pool:
        histories = list(
            pool.map(lambda _: LongTermMemory("alice", str(tmp_path)).get_trip_history(), range(2))
        )
    assert all(len(rows) == 1 for rows in histories)
    assert len(LongTermMemory("alice", str(tmp_path)).get_trip_history(None)) == 1


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
    memory = make_application("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    memory.preferences.set_user_preference("home_location", "杭州")
    memory.preferences.clear_history()
    reopened = make_application("alice", "two", storage_path=str(tmp_path))
    assert reopened.preferences.get_trip_history() == []
    assert reopened.trips.load(workflow["id"]) == workflow
    assert reopened.preferences.get_preference("home_location") == "杭州"
    assert source.read_bytes() == before
    reopened.trips.save(workflow, expected_revision=workflow["revision"])
    assert len(reopened.preferences.get_trip_history()) == 1


def test_compatibility_history_saves_use_unified_storage_and_limit(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.save_trip_history({"destination": "上海"})
    memory.save_trip_history({"destination": "北京"})
    assert [r["destination"] for r in memory.get_trip_history(1)] == ["北京"]
    assert len(memory.get_trip_history(None)) == 2
    assert memory.get_statistics()["total_trips"] == 2
    assert not (resolve_user_directory(tmp_path, "alice") / "trips.json").exists()
    with memory.storage.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM travel_history").fetchone()[0] == 2


def test_summary_reads_migrated_history_when_backup_is_unavailable(tmp_path):
    source = legacy_source(tmp_path, [{"trip_id": "one", "summary": "旧旅行概要"}])
    memory = make_application("alice", "one", storage_path=str(tmp_path))
    # Removing only this test fixture proves the migrated records are self-contained.
    source.unlink()
    memory.trips.list_summaries()
    assert "旧旅行概要" in (resolve_user_directory(tmp_path, "alice") / "trip.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_cli_and_memory_agent_use_completed_workflow_history(tmp_path):
    import io

    from rich.console import Console

    from trip_assistant.cli import TripEvidenceCLI

    memory = make_application("alice", "one", storage_path=str(tmp_path))
    workflow = completed(memory)
    city = workflow["tasks"][-1]["destination"]
    app = TripEvidenceCLI()
    app.app = memory
    output = io.StringIO()
    app.console = Console(file=output, width=200, color_system=None)
    app.show_history()
    assert city in output.getvalue()

    assert city in json.dumps(memory.trips.list_summaries(), ensure_ascii=False)
