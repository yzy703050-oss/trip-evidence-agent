import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest


def storage(root, user="alice"):
    from trip_assistant.storage.sqlite import SQLiteStorage

    return SQLiteStorage(root, user)


def test_transaction_rolls_back(tmp_path):
    db = storage(tmp_path)
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute("INSERT INTO metadata VALUES ('test', 'value')")
        raise RuntimeError("interrupted")
    with db.connection() as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='test'").fetchone() is None
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


@pytest.mark.parametrize("key,value", [("owner", "bob"), ("schema_version", "999")])
def test_database_rejects_foreign_owner_and_newer_schema(tmp_path, key, value):
    db = storage(tmp_path)
    with db.transaction() as conn:
        conn.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))
    with pytest.raises(ValueError):
        storage(tmp_path)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()[0] == value


def test_connections_are_independent(tmp_path):
    db = storage(tmp_path)

    def write(n):
        with db.transaction() as conn:
            conn.execute("INSERT INTO metadata VALUES (?,?)", (str(n), str(n)))
            return conn

    with ThreadPoolExecutor(max_workers=2) as pool:
        connections = list(pool.map(write, range(6)))
    assert len({id(conn) for conn in connections}) == 6
    with db.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM metadata WHERE key IN ('0','1','2','3','4','5')"
            ).fetchone()[0]
            == 6
        )


def test_backup_restores_committed_records(tmp_path):
    db = storage(tmp_path)
    with db.transaction() as conn:
        conn.execute("INSERT INTO metadata VALUES ('test', 'committed')")
    destination = db.backup_to(tmp_path / "backup.sqlite3")
    with sqlite3.connect(destination) as conn:
        assert (
            conn.execute("SELECT value FROM metadata WHERE key='test'").fetchone()[0] == "committed"
        )
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError):
        db.backup_to(db.path)


def test_case_and_unicode_users_have_distinct_databases(tmp_path):
    databases = [storage(tmp_path, user) for user in ("Alice", "alice", "用户甲", "用户乙")]
    assert len({db.path for db in databases}) == 4
