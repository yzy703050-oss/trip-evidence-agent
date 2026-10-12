import json

import pytest

from context.memory_manager import MemoryManager
from workflow_runtime import Runtime, WorkflowMain
from workflow_support import populated_workflow


def test_known_workflows_read_index_without_loading_large_snapshots(tmp_path, monkeypatch):
    memory = MemoryManager("alice", "one", str(tmp_path))
    w = populated_workflow()
    w["tasks"][0]["summary"] = "北京上海旅行小结"
    saved = memory.workflow_store.save(w, expected_revision=None)

    def no_full_read(*args, **kwargs):
        pytest.fail("discovery must not load full workflow JSON")

    monkeypatch.setattr(memory.workflow_store, "load", no_full_read)
    rows = memory.get_known_workflows(query="恢复北京上海的旅行")
    assert rows[0]["id"] == saved["id"] and rows[0]["revision"] == saved["revision"]
    assert "北京上海旅行小结" in json.dumps(rows, ensure_ascii=False)
    assert "results_by_query" not in json.dumps(rows) and "draft_plan" not in json.dumps(rows)
    assert "candidate_cache" not in json.dumps(rows) and "http" not in json.dumps(rows)


def test_saving_one_workflow_does_not_read_other_full_snapshots(tmp_path, monkeypatch):
    memory = MemoryManager("alice", "one", str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(), expected_revision=None)
    memory.get_known_workflows()
    monkeypatch.setattr(
        memory.workflow_store, "list_all", lambda: pytest.fail("incremental save scanned all")
    )
    saved["tasks"][0]["summary"] = "updated"
    updated = memory.workflow_store.save(saved, expected_revision=saved["revision"])
    assert memory.get_known_workflows()[0]["revision"] == updated["revision"]
    assert "updated" in json.dumps(memory.get_known_workflows())


@pytest.mark.asyncio
async def test_resume_loads_only_selected_workflow_after_summary_selection(tmp_path, monkeypatch):
    first = await Runtime(tmp_path, session="one").turn()
    second = await Runtime(tmp_path, session="two").turn()
    target = first["workflow_id"]

    class Main(WorkflowMain):
        async def initialize(self, context):
            assert {w["id"] for w in context["known_workflows"]} == {target, second["workflow_id"]}
            assert "results_by_query" not in json.dumps(context["known_workflows"])
            assert "draft_plan" not in json.dumps(context["known_workflows"])
            return {
                "response_mode": "workflow",
                "finalization_mode": "synthesize",
                "agent_schedule": [],
                "resume_workflow_id": target,
            }

    runtime = Runtime(tmp_path, main=Main(), session="three")
    original = runtime.memory.workflow_store.load
    loaded = []

    def load(identity):
        loaded.append(identity)
        assert identity == target
        return original(identity)

    monkeypatch.setattr(runtime.memory.workflow_store, "load", load)
    result = await runtime.turn("继续第一趟旅行")
    assert result["workflow_id"] == target and result["status"] == "completed"
    assert loaded and set(loaded) == {target}


def test_explicit_index_rebuild_is_user_isolated(tmp_path, monkeypatch):
    memory = MemoryManager('alice','one',str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(),expected_revision=None)
    with memory.workflow_store.storage.transaction() as conn:
        conn.execute("UPDATE trip_plans SET summary_json='{broken'")
    with pytest.raises(ValueError):
        memory.get_known_workflows()
    assert memory.workflow_store.index.rebuild()[0]['id'] == saved['id']
    monkeypatch.setattr(memory.workflow_store,'load',lambda *_: pytest.fail('discovery read full snapshot'))
    assert memory.get_known_workflows()[0]['id'] == saved['id']
    assert MemoryManager('bob','two',str(tmp_path)).get_known_workflows() == []


@pytest.mark.parametrize('broken', ['{}','[]','{broken'])
def test_structurally_corrupt_index_requires_explicit_rebuild(tmp_path, monkeypatch, broken):
    memory = MemoryManager('alice','one',str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(),expected_revision=None)
    with memory.workflow_store.storage.transaction() as conn:
        conn.execute('UPDATE trip_plans SET summary_json=?',(broken,))
    with pytest.raises(ValueError):
        memory.get_known_workflows()
    memory.workflow_store.index.rebuild()
    monkeypatch.setattr(memory.workflow_store,'load',lambda *_: pytest.fail('second discovery read full snapshot'))
    assert memory.get_known_workflows()[0]['revision'] == saved['revision']
