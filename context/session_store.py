"""Indexed SQLite events and revisioned state for user-isolated sessions."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


def storage_component(value: str) -> str:
    """Reject path traversal while allowing existing Unicode user IDs."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError("user_id and session_id must be non-empty path components")
    if any(character in value for character in "/\\:\0<>|?*") or value.endswith((".", " ")):
        raise ValueError("user_id and session_id must be safe path components")
    return value


class SessionStore:
    """Indexed events and revisioned state for one user session."""

    @classmethod
    def list_sessions(cls, root, user_id, limit=20):
        from context.sqlite_storage import SQLiteStorage
        from zoneinfo import ZoneInfo

        db = SQLiteStorage(root, user_id)
        db.ensure_ready()
        with db.connection() as conn:
            values = list(
                conn.execute(
                    "SELECT id,title,updated_at,last_user_message FROM sessions WHERE title<>'' ORDER BY updated_at DESC,id LIMIT ?",
                    (-1 if limit is None else limit,),
                )
            )
        rows = []
        for row in values:
            try:
                timestamp = datetime.fromisoformat(row[2])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                updated = timestamp.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            except (ValueError, TypeError):
                updated = ""
            rows.append(
                {
                    "session_id": row[0],
                    "title": row[1],
                    "updated_at": updated,
                    "last_user_message": row[3],
                }
            )
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)

    def __init__(self, root: str | Path, user_id: str, session_id: str):
        from context.sqlite_storage import SQLiteStorage, ensure_session

        self.user_id = storage_component(user_id)
        self.session_id = storage_component(session_id)
        self.storage = SQLiteStorage(root, user_id)
        self.storage.ensure_ready()
        self.user_dir = self.storage.directory
        with self.storage.transaction() as conn:
            ensure_session(conn, self.session_id)

    def _validate_owner(self, record):
        if (
            not isinstance(record, dict)
            or record.get("user_id") != self.user_id
            or record.get("session_id") != self.session_id
        ):
            raise ValueError("session record identity mismatch")

    def _input(self, value):
        if not isinstance(value, dict):
            raise ValueError("invalid session record")
        if (
            value.get("user_id", self.user_id) != self.user_id
            or value.get("session_id", self.session_id) != self.session_id
        ):
            raise ValueError("session record identity mismatch")
        return {**value, "user_id": self.user_id, "session_id": self.session_id}

    def _decode(self, payload):
        record = json.loads(payload)
        self._validate_owner(record)
        return record

    def read_events(self, *, after_seq=0, scope=None, event_type=None, role=None, turn_id=None):
        clauses, params = ["session_id=?", "seq>?"], [self.session_id, after_seq]
        for field, value in (
            ("scope", scope),
            ("type", event_type),
            ("role", role),
            ("turn_id", turn_id),
        ):
            if value is not None:
                clauses.append(field + "=?")
                params.append(value)
        with self.storage.connection() as conn:
            return [
                self._decode(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM events WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY seq",
                    params,
                )
            ]

    def append(self, event):
        from context.storage_migration import insert_event
        from context.sqlite_storage import ensure_session

        record = self._input(event)
        with self.storage.transaction() as conn:
            if record.get("event_id") is not None:
                previous = conn.execute(
                    "SELECT payload_json FROM events WHERE session_id=? AND event_id=?",
                    (self.session_id, record["event_id"]),
                ).fetchone()
                if previous:
                    return self._decode(previous[0])
            ensure_session(conn, self.session_id)
            record["seq"] = (
                conn.execute(
                    "SELECT last_seq FROM sessions WHERE id=?", (self.session_id,)
                ).fetchone()[0]
                + 1
            )
            record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            insert_event(conn, record)
        return record

    def read_runs(self, *, turn_id=None, event_type=None, after_run_seq=0, through_run_seq=None):
        clauses, params = ["session_id=?"], [self.session_id]
        clauses.append("run_seq>?")
        params.append(after_run_seq)
        if through_run_seq is not None:
            clauses.append("run_seq<=?")
            params.append(through_run_seq)
        for field, value in (("turn_id", turn_id), ("type", event_type)):
            if value is not None:
                clauses.append(field + "=?")
                params.append(value)
        with self.storage.connection() as conn:
            return [
                self._decode(row[0])
                for row in conn.execute(
                    "SELECT payload_json FROM runs WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY run_seq",
                    params,
                )
            ]

    def append_run(self, event):
        from context.sqlite_storage import encode, ensure_session

        record = self._input(event)
        with self.storage.transaction() as conn:
            if record.get("event_id") is not None:
                previous = conn.execute(
                    "SELECT payload_json FROM runs WHERE session_id=? AND event_id=?",
                    (self.session_id, record["event_id"]),
                ).fetchone()
                if previous:
                    return self._decode(previous[0])
            ensure_session(conn, self.session_id)
            record["run_seq"] = (
                conn.execute(
                    "SELECT last_run_seq FROM sessions WHERE id=?", (self.session_id,)
                ).fetchone()[0]
                + 1
            )
            record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            conn.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                (
                    self.session_id,
                    record["run_seq"],
                    record.get("event_id"),
                    record.get("turn_id"),
                    record.get("type"),
                    record.get("task_id"),
                    encode(record),
                ),
            )
            conn.execute(
                "UPDATE sessions SET last_run_seq=? WHERE id=?",
                (record["run_seq"], self.session_id),
            )
        return record

    def _state(self, conn):
        row = conn.execute(
            "SELECT state_json,state_revision FROM sessions WHERE id=?", (self.session_id,)
        ).fetchone()
        if row is None:
            from context.sqlite_storage import metadata

            return {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "state_revision": int(metadata(conn, "state_clock", "0")) + 1,
            }
        state = json.loads(row[0])
        if not isinstance(state, dict):
            raise ValueError("invalid session state")
        if state:
            self._validate_owner(state)
        return {
            **state,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "state_revision": row[1],
        }

    def read_state(self):
        with self.storage.connection() as conn:
            return self._state(conn)

    def _patch(self, conn, patch, remove_keys, expected_revision, *, replace=False):
        from context.sqlite_storage import (
            StateConflictError,
            encode,
            ensure_session,
            next_state_revision,
        )

        patch = self._input(patch)
        ensure_session(conn, self.session_id)
        current = self._state(conn)
        revision = current["state_revision"]
        if expected_revision is not None and revision != expected_revision:
            raise StateConflictError("session state revision changed")
        current = {} if replace else current
        for key in remove_keys:
            current.pop(key, None)
        current.update(patch)
        current["state_revision"] = next_state_revision(conn)
        conn.execute(
            "UPDATE sessions SET state_json=?,state_revision=? WHERE id=?",
            (encode(current), current["state_revision"], self.session_id),
        )
        return current

    def update_state(self, patch, *, remove_keys=(), expected_revision=None):
        with self.storage.transaction() as conn:
            return self._patch(conn, patch, remove_keys, expected_revision)

    def write_state(self, state):
        with self.storage.transaction() as conn:
            self._patch(conn, state, (), state.get("state_revision"), replace=True)

    def add_active_workflow(self, workflow_id):
        storage_component(workflow_id)
        with self.storage.transaction() as conn:
            ids = list(self._state(conn).get("active_workflow_ids", []))
            if workflow_id not in ids:
                ids.append(workflow_id)
            return self._patch(
                conn, {"active_workflow_ids": ids, "active_workflow_id": workflow_id}, (), None
            )

    def latest_seq(self):
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT last_seq FROM sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            return row[0] if row else 0

    def pending_tool_calls(self, *, after_seq=0):
        with self.storage.connection() as conn:
            return [
                {**json.loads(row[0]), "turn_id": row[1], "scope": row[2]}
                for row in conn.execute(
                    "SELECT payload_json,turn_id,scope FROM tool_calls WHERE session_id=? AND result_seq IS NULL AND call_seq>? ORDER BY call_seq,call_id",
                    (self.session_id, after_seq),
                )
            ]

    def latest_message_seq(self):
        with self.storage.connection() as conn:
            return conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE session_id=? AND scope='session' AND type='message' AND role IN ('user','assistant')",
                (self.session_id,),
            ).fetchone()[0]

    def latest_user_turn(self):
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT turn_id FROM events WHERE session_id=? AND scope='session' AND type='message' AND role='user' ORDER BY seq DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            return row[0] if row else None

    def clear_context(self):
        with self.storage.transaction() as conn:
            row = conn.execute(
                "SELECT last_seq FROM sessions WHERE id=?", (self.session_id,)
            ).fetchone()
            seq = row[0] if row else 0
            return self._patch(
                conn,
                {
                    "cleared_through_seq": seq,
                    "covered_through_seq": seq,
                    "summary": "",
                    "session_summary": "",
                    "session_summary_through_seq": seq,
                },
                ("checkpoint",),
                None,
            )
