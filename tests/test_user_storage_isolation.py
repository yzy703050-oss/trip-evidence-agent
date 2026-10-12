import json

import pytest
from workflow_support import populated_workflow

from context.long_term_memory import LongTermMemory
from context.memory_manager import MemoryManager
from context.session_store import SessionStore


def test_case_distinct_users_cannot_share_any_memory(tmp_path):
    first = MemoryManager("Yzy", "s1", storage_path=str(tmp_path))
    first.long_term.set_user_preference("home_location", "重庆")
    first.start_turn("上海出发")
    saved = first.workflow_store.save(populated_workflow(), expected_revision=None)
    first.set_active_workflow(saved["id"])
    other = MemoryManager("yzy", "s1", storage_path=str(tmp_path))
    assert other.long_term.get_preference("home_location") is None
    assert other.session_store.read_events() == []
    assert other.workflow_store.load(saved["id"]) is None
    assert str(first.long_term.user_dir).casefold() != str(other.long_term.user_dir).casefold()


@pytest.mark.parametrize('kind', ['identity','event','state','run'])
def test_foreign_owner_is_rejected(tmp_path, kind):
    store = SessionStore(tmp_path,'alice','s1')
    row = {'user_id':'bob','session_id':'s1','seq':1}
    if kind == 'identity':
        with store.storage.transaction() as conn:
            conn.execute("UPDATE metadata SET value='bob' WHERE key='owner'")
        with pytest.raises(ValueError):
            SessionStore(tmp_path,'alice','s1')
        return
    if kind == 'event':
        store.append({'type':'message','scope':'session','role':'user','content':'q'})
    elif kind == 'run':
        store.append_run({'type':'model_call'})
    with store.storage.transaction() as conn:
        if kind == 'state':
            conn.execute('UPDATE sessions SET state_json=? WHERE id=?', (json.dumps(row),'s1'))
        else:
            table = 'events' if kind == 'event' else 'runs'
            conn.execute('UPDATE '+table+' SET payload_json=?', (json.dumps(row),))
    with pytest.raises(ValueError):
        {'event':store.read_events,'state':store.read_state,'run':store.read_runs}[kind]()


@pytest.mark.parametrize('kind', ['identity','event','state','run'])
def test_non_object_records_are_rejected_as_invalid_data(tmp_path, kind):
    store = SessionStore(tmp_path,'alice','s1')
    if kind == 'event':
        store.append({'type':'message','scope':'session','role':'user','content':'q'})
    elif kind == 'run':
        store.append_run({'type':'model_call'})
    with store.storage.transaction() as conn:
        if kind == 'identity':
            conn.execute("UPDATE metadata SET value='[]' WHERE key='owner'")
        elif kind == 'state':
            conn.execute("UPDATE sessions SET state_json='[]' WHERE id='s1'")
        else:
            conn.execute("UPDATE "+('events' if kind=='event' else 'runs')+" SET payload_json='[]'")
    with pytest.raises(ValueError):
        if kind == 'identity':
            SessionStore(tmp_path,'alice','s1')
        else:
            {'event':store.read_events,'state':store.read_state,'run':store.read_runs}[kind]()


def test_legacy_profile_does_not_claim_unowned_sessions(tmp_path):
    old = tmp_path / "Yzy"
    (old / "sessions" / "s1").mkdir(parents=True)
    profile = {
        "user_id": "Yzy",
        "preferences": [{"type": "home_location", "value": "重庆", "source": "user"}],
    }
    (old / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    event = {"seq": 1, "scope": "session", "role": "user", "content": "旧对话"}
    source = old / "sessions" / "s1" / "events.jsonl"
    source.write_text(json.dumps(event) + "\n", encoding="utf-8")
    before = source.read_bytes()
    memory = MemoryManager("Yzy", "s1", storage_path=str(tmp_path))
    assert memory.long_term.get_preference("home_location") == "重庆"
    assert memory.session_store.read_events() == []
    assert source.read_bytes() == before
    assert (
        MemoryManager("yzy", "s1", storage_path=str(tmp_path)).long_term.get_preference(
            "home_location"
        )
        is None
    )


def test_unicode_identity_and_repeat_initialization_preserve_records(tmp_path):
    store = SessionStore(tmp_path, "用户甲", "s1")
    saved = store.append({"turn_id": "t1", "role": "user", "scope": "session", "content": "北京"})
    assert saved["user_id"] == "用户甲"
    assert SessionStore(tmp_path, "用户甲", "s1").read_events() == [saved]
    assert SessionStore(tmp_path, "用户乙", "s1").read_events() == []


def test_legacy_single_file_migration_is_exact_and_idempotent(tmp_path):
    legacy = {
        "user_id": "Alice",
        "preferences": [],
        "chat_history": [
            {
                "role": "user",
                "content": "去北京",
                "session_id": "old",
                "timestamp": "2026-10-11T08:00:00+08:00",
            }
        ],
    }
    path = tmp_path / "Alice.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    for _ in range(2):
        memory = MemoryManager("Alice", "old", storage_path=str(tmp_path))
        assert len(memory.session_store.read_events()) == 1
    assert (
        MemoryManager("alice", "old", storage_path=str(tmp_path)).session_store.read_events() == []
    )
    assert path.read_bytes() == before


def owned_legacy_session(root):
    path = root / "alice" / "sessions" / "old" / "events.jsonl"
    path.parent.mkdir(parents=True)
    row = {
        "user_id": "alice",
        "session_id": "old",
        "seq": 1,
        "turn_id": "t1",
        "type": "message",
        "scope": "session",
        "role": "user",
        "content": "旧对话",
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def test_owned_legacy_sessions_do_not_resurrect_after_clear(tmp_path):
    source = owned_legacy_session(tmp_path)
    memory = MemoryManager("alice", "old", storage_path=str(tmp_path))
    assert len(memory.session_store.read_events()) == 1
    memory.long_term.clear_history()
    assert (
        MemoryManager("alice", "new", storage_path=str(tmp_path)).long_term.get_chat_history() == []
    )
    assert source.exists()


def test_malformed_unowned_legacy_session_does_not_break_new_identity(tmp_path):
    source = owned_legacy_session(tmp_path)
    source.write_text("{bad}\n", encoding="utf-8")
    assert (
        MemoryManager("alice", "new", storage_path=str(tmp_path)).session_store.read_events() == []
    )


def test_wrong_case_delete_does_not_remove_another_legacy_user(tmp_path):
    source = tmp_path / "Alice.json"
    source.write_text(json.dumps({"user_id": "Alice", "preferences": []}), encoding="utf-8")
    LongTermMemory("alice", str(tmp_path)).delete_all()
    assert source.exists()


def test_failed_profile_import_retries_without_overwriting_newer_profile(tmp_path):
    import sqlite3
    from context.sqlite_storage import SQLiteStorage
    source = tmp_path/'alice/profile.json'
    source.parent.mkdir()
    source.write_text(json.dumps({'user_id':'alice','preferences':[{'type':'home_location','value':'重庆'}]}),encoding='utf-8')
    before = source.read_bytes()
    db = SQLiteStorage(tmp_path,'alice')
    with db.transaction() as conn:
        conn.execute("CREATE TRIGGER fail BEFORE INSERT ON profiles BEGIN SELECT RAISE(ABORT,'interrupted import'); END")
    with pytest.raises(sqlite3.IntegrityError, match='interrupted import'):
        MemoryManager('alice','new',storage_path=str(tmp_path))
    with db.transaction() as conn:
        assert conn.execute('SELECT COUNT(*) FROM preferences').fetchone()[0] == 0
        conn.execute('DROP TRIGGER fail')
    memory = MemoryManager('alice','new',storage_path=str(tmp_path))
    assert memory.long_term.get_preference('home_location') == '重庆'
    memory.long_term.set_user_preference('home_location','北京')
    assert MemoryManager('alice','new',storage_path=str(tmp_path)).long_term.get_preference('home_location') == '北京'
    assert source.read_bytes() == before
