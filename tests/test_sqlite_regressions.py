from copy import deepcopy
import io
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from context.memory_manager import MemoryManager
from context.session_store import SessionStore
from context.sqlite_storage import SQLiteStorage, StateConflictError
from workflow_support import populated_workflow


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "partial", "needs_input"])
async def test_cli_persists_only_slim_final_and_keeps_full_result(tmp_path, status):
    from cli import TripEvidenceCLI

    memory = MemoryManager("alice", "s", str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(), expected_revision=None)
    result = {
        "status": status,
        "finalization_method": "workflow",
        "final_answer": "行程答复",
        "workflow_id": saved["id"],
        "workflow_revision": saved["revision"],
        "workflow": saved,
        "validated_plan": {"tasks": saved["tasks"]},
        "results": [],
    }
    original = deepcopy(result)

    class Harness:
        async def run_turn(self, context, run):
            return result

    app = TripEvidenceCLI()
    app.memory_manager = memory
    app.harness = Harness()
    app.console = Console(file=io.StringIO(), color_system=None)
    assert await app.process_query("安排行程")
    small = json.loads(memory.session_store.read_events()[-1]["content"])
    assert "workflow" not in small and "validated_plan" not in small
    assert small["workflow_revision"] == saved["revision"]
    assert app.last_result is result and result == original
    assert memory.workflow_store.load(saved["id"]) == saved
    assert (
        MemoryManager("alice", "s", str(tmp_path)).get_compacted_context()["recent_messages"][-1][
            "content"
        ]
        == "行程答复"
    )


def test_clear_history_and_recreate_session_cannot_reuse_state_revision(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    state = memory.session_store.update_state({"summary": "old"})
    memory.long_term.clear_history()
    fresh = SessionStore(tmp_path, "alice", "s")
    fresh.update_state({"summary": "new"})
    with pytest.raises(StateConflictError):
        memory.session_store.update_state(
            {"summary": "stale"}, expected_revision=state["state_revision"]
        )
    assert fresh.read_state()["summary"] == "new"


def test_delete_all_rejects_old_async_state_revision(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    old = memory.session_store.read_state()
    memory.long_term.delete_all()
    fresh = SessionStore(tmp_path, "alice", "s")
    with pytest.raises(StateConflictError):
        fresh.update_state({"summary": "stale"}, expected_revision=old["state_revision"])


def test_completed_migration_does_not_depend_on_old_identity(tmp_path):
    from context.user_storage import user_storage_key

    directory = tmp_path / user_storage_key("alice")
    directory.mkdir()
    identity = directory / "identity.json"
    identity.write_text(json.dumps({"user_id": "alice", "version": 1}), encoding="utf-8")
    memory = MemoryManager("alice", "s", str(tmp_path))
    memory.long_term.set_user_preference("seat", "二等座")
    identity.write_text("obsolete damaged source", encoding="utf-8")
    assert MemoryManager("alice", "s", str(tmp_path)).long_term.get_preference("seat") == "二等座"


def test_tool_result_cannot_commit_after_context_clear(tmp_path):
    store = SessionStore(tmp_path, "alice", "s")
    store.append(
        {
            "type": "tool_call",
            "turn_id": "t",
            "scope": "info",
            "tool_calls": [{"id": "c", "name": "weather"}],
        }
    )
    store.clear_context()
    with pytest.raises(ValueError, match="matching"):
        store.append({"type": "tool_result", "turn_id": "t", "scope": "info", "tool_call_id": "c"})
    assert store.latest_seq() == 1


def test_statistics_does_not_decode_full_workflow_or_audit(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    workflow = populated_workflow()
    workflow["status"] = "completed"
    memory.workflow_store.save(workflow, expected_revision=None)
    memory.start_turn("q")
    with memory.long_term.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET content_json='cannot decode'")
        conn.execute("UPDATE events SET payload_json='cannot decode'")
    statistics = memory.long_term.get_statistics()
    assert statistics["total_trips"] == 1 and statistics["total_messages"] == 1


def test_summary_identity_and_revision_mismatch_are_rejected(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(), expected_revision=None)
    summary = memory.workflow_store.list_summaries()[0]
    summary["revision"] += 7
    with memory.long_term.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET summary_json=?", (json.dumps(summary),))
    with pytest.raises(ValueError, match="summary"):
        memory.workflow_store.list_summaries()


def test_database_unavailable_never_falls_back_to_legacy(tmp_path):
    source = tmp_path / "alice.json"
    source.write_text(
        json.dumps({"user_id": "alice", "preferences": {"seat": "二等座"}}), encoding="utf-8"
    )
    memory = MemoryManager("alice", "s", str(tmp_path))
    before = source.read_bytes()
    memory.long_term.storage.path.write_bytes(b"not a database")
    with pytest.raises(Exception, match="database"):
        memory.long_term.get_preference("seat")
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_prior_session_summary_is_not_read_or_restored_after_clear(tmp_path):
    old = MemoryManager("alice", "old", str(tmp_path))
    turn = old.start_turn("酒店偏好" * 20)
    old.record_message("assistant", "酒店答复", turn, final=True)
    current = MemoryManager("alice", "new", str(tmp_path))

    async def model(messages):
        old.clear_short_term()
        return SimpleNamespace(text="stale hotel facts")

    current.llm_model = model
    assert await current.get_long_term_summary_async(query="酒店") == ""
    assert old.session_store.read_state()["session_summary"] == ""


def test_cli_resume_reports_sqlite_error_and_keeps_binding(tmp_path):
    from cli import TripEvidenceCLI

    app = TripEvidenceCLI()
    app.memory_manager = MemoryManager("alice", "s", str(tmp_path))
    app.user_id = "alice"
    app.session_id = "s"
    app.console = Console(file=io.StringIO(), color_system=None)
    before = app.memory_manager
    before.long_term.storage.path.write_bytes(b"not a database")
    assert app.resume_session() is False
    assert app.memory_manager is before
    assert "暂时无法恢复" in app.console.file.getvalue()


@pytest.mark.asyncio
async def test_workflow_sqlite_failure_returns_storage_error(tmp_path, monkeypatch):
    import sqlite3
    from workflow_runtime import Runtime

    runtime = Runtime(tmp_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(runtime.memory.workflow_store, "save", fail)
    result = await runtime.turn()
    assert result["status"] == "error"
    assert result["error_code"] == "workflow_save_failed"
