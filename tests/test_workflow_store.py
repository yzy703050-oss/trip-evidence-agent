from copy import deepcopy
import multiprocessing
from pathlib import Path
import pytest
from context.workflow_store import WorkflowStore, WorkflowConflictError
from context.memory_manager import MemoryManager
from workflow_support import populated_workflow


def test_stale_writer_cannot_overwrite_or_cross_user(tmp_path):
    w = populated_workflow(); a = WorkflowStore(tmp_path, 'alice')
    saved = a.save(w, expected_revision=None)
    stale = a.load(saved['id'])
    newer = WorkflowStore(tmp_path, 'alice').save(saved, expected_revision=saved['revision'])
    with pytest.raises(WorkflowConflictError): a.save(stale, expected_revision=stale['revision'])
    assert a.load(saved['id']) == newer
    assert WorkflowStore(tmp_path, 'bob').load(saved['id']) is None


def test_restart_preserves_candidates_and_checkpoint(tmp_path):
    memory = MemoryManager('alice', 's', storage_path=str(tmp_path))
    w = populated_workflow(); w['status'] = 'needs_input'
    w['checkpoint'] = {'question': '修改第二段日期吗？', 'resume_task_id': w['tasks'][1]['id']}
    saved = memory.workflow_store.save(w, expected_revision=None)
    memory.set_active_workflow(saved['id'])
    restarted = MemoryManager('alice', 's', storage_path=str(tmp_path))
    restored = restarted.get_active_workflows()[0]
    assert restored == saved
    assert next(iter(restored['results_by_query'].values()))['items'][0]['source']
    assert restarted.session_store.read_state()['active_workflow_ids'] == [saved['id']]
    assert 'tasks' not in restarted.session_store.read_state()


@pytest.mark.parametrize('bad', ['../escape', 'a/b', 'a\\b', 'C:bad', '..'])
def test_workflow_path_is_validated(tmp_path, bad):
    with pytest.raises(ValueError): WorkflowStore(tmp_path, 'alice').load(bad)


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    import context.workflow_store as module
    store = WorkflowStore(tmp_path, 'alice'); w = store.save(populated_workflow(), expected_revision=None)
    def fail(*args): raise OSError('offline write failure')
    monkeypatch.setattr(module, 'atomic_json_write', fail)
    with pytest.raises(OSError): store.save(w, expected_revision=w['revision'])
    assert store.load(w['id']) == w


def _writer(root, workflow_id, revision, queue):
    store = WorkflowStore(root, 'alice'); w = store.load(workflow_id)
    try:
        store.save(w, expected_revision=revision); queue.put('saved')
    except WorkflowConflictError: queue.put('conflict')


def test_two_processes_cannot_both_commit_same_revision(tmp_path):
    store = WorkflowStore(tmp_path, 'alice'); w = store.save(populated_workflow(), expected_revision=None)
    ctx = multiprocessing.get_context('spawn'); queue = ctx.Queue()
    workers = [ctx.Process(target=_writer, args=(str(tmp_path), w['id'], w['revision'], queue)) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(20); assert worker.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in workers) == ['conflict', 'saved']
    assert store.load(w['id'])['revision'] == w['revision'] + 1


def test_summary_update_does_not_remove_active_index(tmp_path):
    memory = MemoryManager('alice', 's', storage_path=str(tmp_path))
    w = memory.workflow_store.save(populated_workflow(), expected_revision=None)
    memory.set_active_workflow(w['id'])
    state = memory.session_store.read_state(); state['summary'] = '压缩摘要'
    memory.session_store.write_state(state)
    assert memory.get_active_workflows()[0] == w
