import json
import pytest
from context.memory_manager import MemoryManager
from workflow_runtime import Runtime
from workflow_support import populated_workflow


def test_partial_and_completed_are_known_across_sessions_and_summary_repairs(tmp_path):
    m=MemoryManager('alice','one',storage_path=str(tmp_path))
    w=populated_workflow(); w['status']='partial'
    saved=m.workflow_store.save(w,expected_revision=None)
    path=tmp_path/'alice'/'trip.md'
    assert path.exists() and saved['id'] in path.read_text(encoding='utf-8')
    saved['status']='completed'
    saved=m.workflow_store.save(saved,expected_revision=saved['revision'])
    path.write_text('过时摘要 revision=0',encoding='utf-8')
    newer=MemoryManager('alice','two',storage_path=str(tmp_path))
    assert newer.get_known_workflows()[0]['status']=='completed'
    text=path.read_text(encoding='utf-8')
    assert f"revision: {saved['revision']}" in text and 'planned_itinerary' in text
    assert '过时摘要' not in text
    assert not MemoryManager('bob','two',storage_path=str(tmp_path)).get_known_workflows()


def test_legacy_projection_is_idempotent_and_preserves_json(tmp_path):
    m=MemoryManager('alice','one',storage_path=str(tmp_path))
    m.long_term.save_trip_history(dict(origin='重庆',destination='上海',summary='旧记录'))
    before=(tmp_path/'alice'/'trips.json').read_bytes()
    m.get_known_workflows(); m.get_known_workflows()
    assert (tmp_path/'alice'/'trips.json').read_bytes()==before
    text=(tmp_path/'alice'/'trip.md').read_text(encoding='utf-8')
    assert text.count('旧记录')==1


@pytest.mark.asyncio
async def test_replanning_same_trip_does_not_duplicate_history_or_statistics(tmp_path):
    r=Runtime(tmp_path); first=await r.turn()
    for i in range(2):
        r.main.resume=dict(id=first['workflow_id'],update={'task_updates':[]})
        await r.turn('继续')
    history=r.memory.long_term.get_trip_history(20)
    assert len([x for x in history if x.get('workflow_id')==first['workflow_id']])==1
    assert r.memory.long_term.get_statistics()['total_trips']==1
    assert (tmp_path/'alice'/'trip.md').read_text(encoding='utf-8').count('## '+first['workflow_id'])==1


def test_projection_failure_does_not_fail_canonical_save(tmp_path,monkeypatch):
    from context.trip_memory import TripMemory
    m=MemoryManager('alice','one',storage_path=str(tmp_path))
    def fail(*args,**kwargs): raise OSError('test readonly summary')
    monkeypatch.setattr(TripMemory,'rebuild',fail)
    saved=m.workflow_store.save(populated_workflow(),expected_revision=None)
    assert m.workflow_store.load(saved['id'])==saved
