"""Offline regression scenarios grouped by responsibility."""

import json
import multiprocessing

import pytest
from support.native import active_trips, make_application
from support.workflow import populated_workflow

from trip_assistant.storage.trips import TripStore, WorkflowConflictError

# workflow_store


def test_stale_writer_cannot_overwrite_or_cross_user(tmp_path):
    w = populated_workflow()
    a = TripStore(tmp_path, "alice")
    saved = a.save(w, expected_revision=None)
    stale = a.load(saved["id"])
    newer = TripStore(tmp_path, "alice").save(saved, expected_revision=saved["revision"])
    with pytest.raises(WorkflowConflictError):
        a.save(stale, expected_revision=stale["revision"])
    assert a.load(saved["id"]) == newer
    assert TripStore(tmp_path, "bob").load(saved["id"]) is None


def test_restart_preserves_candidates_and_checkpoint(tmp_path):
    memory = make_application("alice", "s", storage_path=str(tmp_path))
    w = populated_workflow()
    w["status"] = "needs_input"
    w["checkpoint"] = {"question": "修改第二段日期吗？", "resume_task_id": w["tasks"][1]["id"]}
    saved = memory.trips.save(w, expected_revision=None)
    memory.sessions.add_active_workflow(saved["id"])
    restarted = make_application("alice", "s", storage_path=str(tmp_path))
    restored = active_trips(restarted)[0]
    assert restored == saved
    assert next(iter(restored["results_by_query"].values()))["items"][0]["source"]
    assert restarted.sessions.read_state()["active_workflow_ids"] == [saved["id"]]
    assert "tasks" not in restarted.sessions.read_state()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "C:bad", ".."])
def test_workflow_path_is_validated(tmp_path, bad):
    with pytest.raises(ValueError):
        TripStore(tmp_path, "alice").load(bad)


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    import sqlite3

    store = TripStore(tmp_path, "alice")
    w = store.save(populated_workflow(), expected_revision=None)
    with store.storage.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail BEFORE UPDATE ON trip_plans "
            "BEGIN SELECT RAISE(ABORT,'offline write failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="offline write failure"):
        store.save(w, expected_revision=w["revision"])
    assert store.load(w["id"]) == w


def _writer(root, workflow_id, revision, queue):
    store = TripStore(root, "alice")
    w = store.load(workflow_id)
    try:
        store.save(w, expected_revision=revision)
        queue.put("saved")
    except WorkflowConflictError:
        queue.put("conflict")


def test_two_processes_cannot_both_commit_same_revision(tmp_path):
    store = TripStore(tmp_path, "alice")
    w = store.save(populated_workflow(), expected_revision=None)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [
        ctx.Process(target=_writer, args=(str(tmp_path), w["id"], w["revision"], queue))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ["conflict", "saved"]
    assert store.load(w["id"])["revision"] == w["revision"] + 1


def test_summary_update_does_not_remove_active_index(tmp_path):
    memory = make_application("alice", "s", storage_path=str(tmp_path))
    w = memory.trips.save(populated_workflow(), expected_revision=None)
    memory.sessions.add_active_workflow(w["id"])
    state = memory.sessions.read_state()
    state["summary"] = "压缩摘要"
    memory.sessions.write_state(state)
    assert active_trips(memory)[0] == w


# workflow_summary_index


def test_known_workflows_read_index_without_loading_large_snapshots(tmp_path, monkeypatch):
    memory = make_application("alice", "one", str(tmp_path))
    w = populated_workflow()
    w["tasks"][0]["summary"] = "北京上海旅行小结"
    saved = memory.trips.save(w, expected_revision=None)

    def no_full_read(*args, **kwargs):
        pytest.fail("discovery must not load full workflow JSON")

    monkeypatch.setattr(memory.trips, "load", no_full_read)
    rows = memory.trips.list_summaries()
    assert rows[0]["id"] == saved["id"] and rows[0]["revision"] == saved["revision"]
    assert "北京上海旅行小结" in json.dumps(rows, ensure_ascii=False)
    assert "results_by_query" not in json.dumps(rows) and "draft_plan" not in json.dumps(rows)
    assert "candidate_cache" not in json.dumps(rows) and "http" not in json.dumps(rows)


def test_saving_one_workflow_does_not_read_other_full_snapshots(tmp_path, monkeypatch):
    memory = make_application("alice", "one", str(tmp_path))
    saved = memory.trips.save(populated_workflow(), expected_revision=None)
    memory.trips.list_summaries()
    monkeypatch.setattr(
        memory.trips, "list_all", lambda: pytest.fail("incremental save scanned all")
    )
    saved["tasks"][0]["summary"] = "updated"
    updated = memory.trips.save(saved, expected_revision=saved["revision"])
    assert memory.trips.list_summaries()[0]["revision"] == updated["revision"]
    assert "updated" in json.dumps(memory.trips.list_summaries())


@pytest.mark.asyncio
async def test_resume_loads_only_selected_workflow_after_summary_selection(tmp_path, monkeypatch):
    from copy import deepcopy

    from trip_assistant.tools.read import build_read_handler

    memory = make_application("alice", "one", str(tmp_path))
    first = memory.trips.save(populated_workflow(), expected_revision=None)
    other = deepcopy(first)
    other["id"] = "other-trip"
    memory.trips.save(other, expected_revision=None)
    calls = []
    load = memory.trips.load

    def selected(identity):
        calls.append(identity)
        assert identity == first["id"]
        return load(identity)

    monkeypatch.setattr(memory.trips, "load", selected)
    read = build_read_handler(memory.trips, memory.evidence)
    rows = await read({"resource": "trip_summaries"}, {})
    assert len(rows["data"]["summaries"]) == 2 and calls == []
    detail = await read({"resource": "trip", "trip_plan_id": first["id"]}, {})
    assert detail["data"]["id"] == first["id"] and calls == [first["id"]]


def test_explicit_index_rebuild_is_user_isolated(tmp_path, monkeypatch):
    memory = make_application("alice", "one", str(tmp_path))
    saved = memory.trips.save(populated_workflow(), expected_revision=None)
    with memory.trips.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET summary_json='{broken'")
    with pytest.raises(ValueError):
        memory.trips.list_summaries()
    assert memory.trips.index.rebuild()[0]["id"] == saved["id"]
    monkeypatch.setattr(
        memory.trips, "load", lambda *_: pytest.fail("discovery read full snapshot")
    )
    assert memory.trips.list_summaries()[0]["id"] == saved["id"]
    assert make_application("bob", "two", str(tmp_path)).trips.list_summaries() == []


@pytest.mark.parametrize("broken", ["{}", "[]", "{broken"])
def test_structurally_corrupt_index_requires_explicit_rebuild(tmp_path, monkeypatch, broken):
    memory = make_application("alice", "one", str(tmp_path))
    saved = memory.trips.save(populated_workflow(), expected_revision=None)
    with memory.trips.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET summary_json=?", (broken,))
    with pytest.raises(ValueError):
        memory.trips.list_summaries()
    memory.trips.index.rebuild()
    monkeypatch.setattr(
        memory.trips, "load", lambda *_: pytest.fail("second discovery read full snapshot")
    )
    assert memory.trips.list_summaries()[0]["revision"] == saved["revision"]
