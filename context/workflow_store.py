"""Transactional workflow snapshots, summaries and compatible travel history."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
import hashlib
import os
from pathlib import Path
import threading
import uuid

from context.session_store import storage_component

_guard = threading.Lock()
_locks = {}


class WorkflowConflictError(ValueError):
    """A newer workflow revision exists; never replay queries to resolve this."""


@contextmanager
def _file_lock(path):
    key = path.resolve()
    with _guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class WorkflowStore:
    def __init__(self, root, user_id):
        from context.sqlite_storage import SQLiteStorage
        from context.trip_memory import TripMemory
        from context.workflow_index import WorkflowIndex

        self.user_id = storage_component(user_id)
        self.storage = SQLiteStorage(root, user_id)
        self.storage.ensure_ready()
        self.directory = self.storage.directory / "workflows"  # legacy input only
        self.trip_memory = TripMemory(root, user_id)
        self.index = WorkflowIndex(self)

    def load(self, workflow_id):
        storage_component(workflow_id)
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT content_json FROM trip_plans WHERE id=?", (workflow_id,)
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row[0])
            self._validate(value, workflow_id)
            from agents.workflow_state import normalize_workflow_states
            return normalize_workflow_states(value)

    def _validate(self, value, identity):
        if not isinstance(value, dict) or value.get("user_id") != self.user_id:
            raise ValueError("workflow owner mismatch")
        if (
            value.get("id") != identity
            or type(value.get("revision")) is not int
            or not isinstance(value.get("tasks"), list)
        ):
            raise ValueError("invalid workflow snapshot")

    def save(self, workflow, *, expected_revision):
        from context.sqlite_storage import encode
        from context import workflow_index

        from agents.workflow_state import normalize_workflow_states
        value = normalize_workflow_states(workflow)
        if value.get("user_id", self.user_id) != self.user_id:
            raise ValueError("workflow owner mismatch")
        value["user_id"] = self.user_id
        identity = storage_component(value["id"])
        with self.storage.transaction() as conn:
            current = conn.execute(
                "SELECT revision FROM trip_plans WHERE id=?", (identity,)
            ).fetchone()
            if current is None:
                if expected_revision is not None:
                    raise WorkflowConflictError("workflow no longer exists")
                if conn.execute("SELECT 1 FROM travel_history WHERE id=?", (identity,)).fetchone():
                    raise WorkflowConflictError(
                        "history record cannot become an executable workflow"
                    )
                revision = 1
            else:
                if type(expected_revision) is not int or current[0] != expected_revision:
                    raise WorkflowConflictError("workflow revision changed")
                revision = expected_revision + 1
            value["revision"] = revision
            self._validate(value, identity)
            now = datetime.now(timezone.utc).isoformat()
            value.setdefault("created_at", now)
            value["updated_at"] = now
            summary = encode(workflow_index.workflow_summary(value))
            content = encode(value)
            if current:
                changed = conn.execute(
                    "UPDATE trip_plans SET revision=?,status=?,updated_at=?,summary_json=?,content_json=? WHERE id=? AND revision=?",
                    (
                        revision,
                        value.get("status"),
                        now,
                        summary,
                        content,
                        identity,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise WorkflowConflictError("workflow revision changed")
            else:
                conn.execute(
                    "INSERT INTO trip_plans VALUES (?,?,?,?,?,?,?)",
                    (
                        identity,
                        revision,
                        value.get("status"),
                        value["created_at"],
                        now,
                        summary,
                        content,
                    ),
                )
        self.trip_memory.repair(self)
        return value

    def list_summaries(self):
        return self.index.list()

    def list_all(self):
        with self.storage.connection() as conn:
            values = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT content_json FROM trip_plans ORDER BY updated_at DESC,id"
                )
            ]
        for value in values:
            self._validate(value, value.get("id"))
        return values

    def list_active(self):
        with self.storage.connection() as conn:
            values = [
                json.loads(row[0])
                for row in conn.execute(
                    "SELECT content_json FROM trip_plans WHERE status IN ('running','needs_input','partial') ORDER BY updated_at DESC,id"
                )
            ]
        for value in values:
            self._validate(value, value.get("id"))
        from agents.workflow_state import normalize_workflow_states, STOPPED
        values = [normalize_workflow_states(v) for v in values]
        values = [v for v in values if v.get('stop_reason') not in STOPPED]
        return values

    def _write_history(self, conn, record, *, importing=False, import_index=None):
        from context.storage_migration import history_identity
        from context.sqlite_storage import encode

        if not isinstance(record, dict):
            raise ValueError("invalid travel history")
        row = deepcopy(record)
        if not importing and not row.get("workflow_id") and not row.get("trip_id"):
            row["trip_id"] = "trip_" + uuid.uuid4().hex
        record_id = history_identity(row, import_index)
        current = conn.execute(
            "SELECT revision,payload_json FROM travel_history WHERE id=?", (record_id,)
        ).fetchone()
        if current and importing:
            return
        if current:
            row = {**json.loads(current[1]), **row}
        now = datetime.now(timezone.utc).isoformat()
        if not importing:
            row.setdefault("trip_id", record_id)
            row.setdefault("timestamp", now)
        conn.execute(
            "INSERT INTO travel_history VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,updated_at=excluded.updated_at,payload_json=excluded.payload_json",
            (record_id, current[0] + 1 if current else 1, now, encode(row)),
        )

    def save_history(self, record):
        if not isinstance(record, dict):
            raise ValueError("invalid travel history")
        with self.storage.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM trip_plans WHERE id=?", (record.get("workflow_id"),)
            ).fetchone():
                return
            self._write_history(conn, record)

    def import_history(self, records, source):
        from context.sqlite_storage import metadata, set_metadata

        with self.storage.transaction() as conn:
            key = "history_import:" + source
            if metadata(conn, key) == "1":
                return
            if not isinstance(records, list):
                raise ValueError("invalid legacy travel history")
            latest = {}
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ValueError("invalid legacy travel record")
                key_id = self._legacy_key(record)
                revision = record.get("workflow_revision", 0)
                revision = revision if type(revision) is int else 0
                if key_id is not None and (key_id not in latest or revision >= latest[key_id][1]):
                    latest[key_id] = (index, revision)
            for index, record in enumerate(records):
                key_id = self._legacy_key(record)
                if key_id is not None and index != latest[key_id][0]:
                    continue
                if not conn.execute(
                    "SELECT 1 FROM trip_plans WHERE id=?", (record.get("workflow_id"),)
                ).fetchone():
                    self._write_history(conn, record, importing=True, import_index=index)
            set_metadata(conn, key, "1")

    @staticmethod
    def _legacy_key(record):
        for field in ("workflow_id", "trip_id"):
            if record.get(field):
                return json.dumps([field, record[field]], ensure_ascii=False, sort_keys=True)
        return None

    @staticmethod
    def _history_view(workflow):
        tasks = workflow["tasks"]
        conditions = workflow.get("confirmed_conditions", {})
        return {
            "trip_id": workflow["id"],
            "workflow_id": workflow["id"],
            "workflow_revision": workflow["revision"],
            "record_kind": "planned_itinerary",
            "timestamp": workflow.get("updated_at", ""),
            "origin": tasks[0].get("origin") or "" if tasks else "",
            "destination": tasks[-1].get("destination") or "" if tasks else "",
            "start_date": conditions.get("start_date") or "",
            "end_date": conditions.get("end_date") or "",
            "purpose": tasks[0].get("purpose") or "" if tasks else "",
            "itinerary": deepcopy(workflow.get("validation", {})),
            "summary": "\n".join(t.get("summary") or "" for t in tasks).strip(),
        }

    def list_history(self, limit=None, *, include_workflows=True):
        from context.sqlite_storage import metadata

        with self.storage.connection() as conn:
            conn.execute("BEGIN")
            cutoff = metadata(conn, "history_cleared_before", "")
            entries = []
            if include_workflows:
                for row in conn.execute(
                    "SELECT updated_at,content_json FROM trip_plans WHERE status='completed' AND updated_at>?",
                    (cutoff,),
                ):
                    value = json.loads(row[1])
                    self._validate(value, value.get("id"))
                    entries.append((row[0], self._history_view(value)))
            for row in conn.execute(
                "SELECT updated_at,payload_json FROM travel_history WHERE updated_at>? ORDER BY updated_at,id",
                (cutoff,),
            ):
                value = json.loads(row[1])
                if not conn.execute(
                    "SELECT 1 FROM trip_plans WHERE id=?", (value.get("workflow_id"),)
                ).fetchone():
                    entries.append((row[0], value))
            rows = [row for _, row in sorted(entries, key=lambda pair: pair[0])]
            return rows[-limit:] if limit else rows

    def clear_history(self):
        from context.sqlite_storage import set_metadata

        with self.storage.transaction() as conn:
            set_metadata(conn, "history_cleared_before", datetime.now(timezone.utc).isoformat())
