"""Per-user SQLite connections and short, explicit transactions."""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from context.user_storage import user_storage_key

SCHEMA_VERSION = 1


class StateConflictError(ValueError):
    """An asynchronous writer observed an older session state."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def metadata(conn, key, default=None):
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_metadata(conn, key, value):
    conn.execute(
        "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def next_state_revision(conn):
    revision = int(metadata(conn, "state_clock", "0")) + 1
    set_metadata(conn, "state_clock", revision)
    return revision


def ensure_session(conn, session_id):
    if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
        conn.execute(
            "INSERT INTO sessions(id,state_revision) VALUES (?,?)",
            (session_id, next_state_revision(conn)),
        )


DDL = (
    "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY CHECK(id=1), payload_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS preferences (type TEXT PRIMARY KEY, value_json TEXT NOT NULL, source TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0)",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, state_json TEXT NOT NULL DEFAULT '{}', state_revision INTEGER NOT NULL DEFAULT 0,
        last_seq INTEGER NOT NULL DEFAULT 0, last_run_seq INTEGER NOT NULL DEFAULT 0,
        title TEXT NOT NULL DEFAULT '', last_user_message TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS events (
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, seq INTEGER NOT NULL,
        event_id TEXT, turn_id TEXT, scope TEXT, type TEXT, role TEXT, timestamp TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(session_id,seq), UNIQUE(session_id,event_id))""",
    "CREATE INDEX IF NOT EXISTS events_context ON events(session_id,scope,type,seq)",
    "CREATE INDEX IF NOT EXISTS events_turn ON events(session_id,turn_id,seq)",
    "CREATE INDEX IF NOT EXISTS events_messages ON events(scope,type,role,timestamp,session_id,seq)",
    "CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated_at DESC,id)",
    """CREATE TABLE IF NOT EXISTS tool_calls (
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, call_id TEXT NOT NULL,
        call_seq INTEGER NOT NULL, result_seq INTEGER, turn_id TEXT NOT NULL, scope TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(session_id,call_id))""",
    "CREATE INDEX IF NOT EXISTS tool_calls_pending ON tool_calls(session_id,result_seq,call_seq)",
    """CREATE TABLE IF NOT EXISTS runs (
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, run_seq INTEGER NOT NULL,
        event_id TEXT, turn_id TEXT, type TEXT, task_id TEXT, payload_json TEXT NOT NULL,
        PRIMARY KEY(session_id,run_seq), UNIQUE(session_id,event_id))""",
    "CREATE INDEX IF NOT EXISTS runs_turn ON runs(session_id,turn_id,type,run_seq)",
    """CREATE TABLE IF NOT EXISTS trip_plans (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT, created_at TEXT, updated_at TEXT,
        summary_json TEXT NOT NULL, content_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS travel_history (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, updated_at TEXT, payload_json TEXT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS migration_sources (path TEXT PRIMARY KEY, digest TEXT NOT NULL, manifest_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS trip_plans_updated ON trip_plans(updated_at DESC,id)",
    "CREATE INDEX IF NOT EXISTS trip_plans_status ON trip_plans(status,updated_at)",
    "CREATE INDEX IF NOT EXISTS travel_history_updated ON travel_history(updated_at,id)",
)


class SQLiteStorage:
    def __init__(self, root: str | Path, user_id: str):
        self.root, self.user_id = Path(root), user_id
        self.directory = self.root / user_storage_key(user_id)
        self.path = self.directory / "agent.sqlite3"
        self.directory.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
            ).fetchone()
            if exists:
                self._validate(conn)
            elif conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError("unknown database schema")
            for sql in DDL:
                conn.execute(sql)
            if not exists:
                set_metadata(conn, "owner", self.user_id)
                set_metadata(conn, "schema_version", SCHEMA_VERSION)
            if metadata(conn, "state_clock") is None:
                set_metadata(
                    conn,
                    "state_clock",
                    conn.execute("SELECT COALESCE(MAX(state_revision),0) FROM sessions").fetchone()[
                        0
                    ],
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            # SQLite defaults to DELETE for a new database; don't silently change
            # an existing database's journal mode on every read connection.
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise ValueError("unsupported database journal mode")
            return conn
        except BaseException:
            conn.close()
            raise

    def _validate(self, conn):
        if metadata(conn, "owner") != self.user_id:
            raise ValueError("database owner mismatch")
        if metadata(conn, "schema_version") != str(SCHEMA_VERSION):
            raise ValueError("unsupported database schema version")

    @contextmanager
    def connection(self):
        conn = self._connect()
        try:
            self._validate(conn)
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._validate(conn)
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def ensure_ready(self):
        from context.storage_migration import migrate_user

        migrate_user(self)

    def backup_to(self, destination: str | Path) -> Path:
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("backup must differ from source")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".sqlite-backup-", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with self.connection() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination
