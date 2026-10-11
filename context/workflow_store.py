"""User-isolated workflow snapshots with atomic writes and process-safe CAS."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import threading
import uuid

from context.session_store import atomic_json_write, storage_component

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
        from context.user_storage import resolve_user_directory
        self.user_id = storage_component(user_id)
        self.directory = resolve_user_directory(root, user_id) / "workflows"
        from context.trip_memory import TripMemory

        self.trip_memory = TripMemory(root, user_id)

    def _path(self, workflow_id):
        return self.directory / f"{storage_component(workflow_id)}.json"

    @staticmethod
    def _read(path):
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("record_type") == "travel_history":
            if type(value.get("revision")) is not int or not isinstance(value.get("history"), dict):
                raise ValueError("invalid travel history record")
            return value
        if (
            not isinstance(value, dict)
            or type(value.get("revision")) is not int
            or not isinstance(value.get("tasks"), list)
        ):
            raise ValueError("invalid workflow snapshot")
        return value

    def load(self, workflow_id):
        path = self._path(workflow_id)
        with _file_lock(path.with_suffix(".lock")):
            value = self._read(path)
            if value is not None and value.get('user_id') != self.user_id:
                raise ValueError('workflow owner mismatch')
            if value is not None and value.get("id") != workflow_id:
                raise ValueError("workflow snapshot identity mismatch")
            if value and value.get("record_type") == "travel_history":
                return None
            return value

    def save(self, workflow, *, expected_revision):
        value = deepcopy(workflow)
        if value.get('user_id', self.user_id) != self.user_id:
            raise ValueError('workflow owner mismatch')
        value['user_id'] = self.user_id
        path = self._path(value["id"])
        with _file_lock(path.with_suffix(".lock")):
            current = self._read(path)
            if current is not None and current.get('user_id') != self.user_id:
                raise ValueError('workflow owner mismatch')
            if current and current.get("record_type") == "travel_history":
                raise WorkflowConflictError("history record cannot become an executable workflow")
            if current is None:
                if expected_revision is not None:
                    raise WorkflowConflictError("workflow no longer exists")
                revision = 1
            else:
                if type(expected_revision) is not int or current["revision"] != expected_revision:
                    raise WorkflowConflictError("workflow revision changed")
                revision = expected_revision + 1
            value["revision"] = revision
            value.setdefault("created_at", datetime.now(timezone.utc).isoformat())
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
            atomic_json_write(path, value)
        self.trip_memory.repair(self)
        return value

    def list_all(self):
        if not self.directory.exists():
            return []
        values = [
            value
            for path in sorted(self.directory.glob("*.json"))
            if path.name != ".history-state.json" and (value := self.load(path.stem))
        ]
        return sorted(
            values,
            key=lambda w: w.get("updated_at") or str(self._path(w["id"]).stat().st_mtime),
            reverse=True,
        )

    def list_active(self):
        if not self.directory.exists():
            return []
        values = self.list_all()
        return [
            value
            for value in values
            if value and value["status"] in {"running", "needs_input", "partial"}
        ]

    def _history_state(self):
        path = self.directory / ".history-state.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _write_history(self, record, *, importing=False, import_index=None):
        if not isinstance(record, dict):
            raise ValueError("invalid travel history")
        row = deepcopy(record)
        # Hash external identities rather than using them as filesystem paths.
        if row.get("workflow_id"):
            identity = ["workflow", row["workflow_id"]]
        elif row.get("trip_id"):
            identity = ["trip", row["trip_id"]]
        else:
            if importing:
                identity = ["legacy", import_index, row]
            else:
                row["trip_id"] = "trip_" + uuid.uuid4().hex
                identity = ["trip", row["trip_id"]]
        digest = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        record_id = "history_" + digest
        path = self._path(record_id)
        current = self._read(path)
        if current:
            if current.get("record_type") != "travel_history":
                raise WorkflowConflictError("history identity collides with workflow")
            if importing:
                return
            row = {**current["history"], **row}
        now = datetime.now(timezone.utc).isoformat()
        if not importing:
            row.setdefault("trip_id", record_id)
            row.setdefault("timestamp", now)
        atomic_json_write(
            path,
            {
                "id": record_id,
                "user_id": self.user_id,
                "record_type": "travel_history",
                "revision": current["revision"] + 1 if current else 1,
                "updated_at": now,
                "history": row,
            },
        )

    def save_history(self, record):
        if not isinstance(record, dict):
            raise ValueError("invalid travel history")
        with _file_lock(self.directory / ".history.lock"):
            if any(w["id"] == record.get("workflow_id") for w in self.list_all()):
                return  # The canonical workflow already holds the latest plan.
            self._write_history(record)

    def import_history(self, records, source):
        with _file_lock(self.directory / ".history.lock"):
            state = self._history_state()
            imported = state.get("imported_sources", [])
            if source in imported:
                return
            if not isinstance(records, list):
                raise ValueError("invalid legacy travel history")
            latest = {}
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    raise ValueError("invalid legacy travel record")
                key = self._legacy_key(record)
                revision = record.get("workflow_revision", 0)
                revision = revision if type(revision) is int else 0
                if key is not None and (key not in latest or revision >= latest[key][1]):
                    latest[key] = (index, revision)
            ids = {w["id"] for w in self.list_all()}
            for index, record in enumerate(records):
                key = self._legacy_key(record)
                if key is not None and index != latest[key][0]:
                    continue
                if record.get("workflow_id") not in ids:
                    self._write_history(record, importing=True, import_index=index)
            state["imported_sources"] = [*imported, source]
            atomic_json_write(self.directory / ".history-state.json", state)

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

    def list_history(self, limit=None):
        with _file_lock(self.directory / ".history.lock"):
            cutoff = self._history_state().get("cleared_before", "")
            workflows = self.list_all()
            ids = {w["id"] for w in workflows}
            entries = [
                (w.get("updated_at", ""), self._history_view(w))
                for w in workflows
                if w["status"] == "completed"
            ]
            for path in sorted(self.directory.glob("history_*.json")):
                with _file_lock(path.with_suffix(".lock")):
                    value = self._read(path)
                if value and value.get("record_type") == "travel_history":
                    if value.get("id") != path.stem:
                        raise ValueError("travel history identity mismatch")
                    row = value["history"]
                    if row.get("workflow_id") not in ids:
                        entries.append((value["updated_at"], deepcopy(row)))
            rows = [
                row
                for updated, row in sorted(entries, key=lambda pair: pair[0])
                if updated > cutoff
            ]
            return rows[-limit:] if limit else rows

    def clear_history(self):
        with _file_lock(self.directory / ".history.lock"):
            state = self._history_state()
            state["cleared_before"] = datetime.now(timezone.utc).isoformat()
            atomic_json_write(self.directory / ".history-state.json", state)
