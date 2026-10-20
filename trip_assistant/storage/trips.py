"""Transactional workflow snapshots, summaries and compatible travel history."""

import json
import uuid
from copy import deepcopy
from datetime import UTC, datetime

from trip_assistant.storage.identity import storage_component
from trip_assistant.storage.locking import _file_lock as _file_lock


class WorkflowConflictError(ValueError):
    """A newer workflow revision exists; never replay queries to resolve this."""

class TripStore:
    def record_unchanged(self, workflow, operation_id):
        """A validated no-op still has an idempotent receipt, without a new business version."""
        from trip_assistant.storage.sqlite import encode

        self._validate(workflow, workflow["id"])
        with self.storage.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM trip_plans WHERE id=?", (workflow["id"],)
            ).fetchone()
            if not row or row[0] != workflow["revision"]:
                raise WorkflowConflictError("trip revision changed")
            conn.execute(
                "INSERT INTO operation_receipts VALUES (?,?,?)",
                (operation_id, workflow["id"], encode(workflow)),
            )
        return deepcopy(workflow)

    def __init__(self, root, user_id):
        from trip_assistant.storage.index import WorkflowIndex
        from trip_assistant.storage.legacy_mirror import TripMemory
        from trip_assistant.storage.sqlite import SQLiteStorage

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
            from trip_assistant.trips.state import normalize_workflow_states

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

    def save(self, workflow, *, expected_revision, operation_id=None):
        from trip_assistant.storage import index
        from trip_assistant.storage.sqlite import encode
        from trip_assistant.trips.state import normalize_workflow_states

        value = normalize_workflow_states(workflow)
        if value.get("user_id", self.user_id) != self.user_id:
            raise ValueError("workflow owner mismatch")
        value["user_id"] = self.user_id
        identity = storage_component(value["id"])
        with self.storage.transaction() as conn:
            if operation_id:
                previous = conn.execute(
                    "SELECT trip_id,result_json FROM operation_receipts WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if previous:
                    if previous[0] != identity:
                        raise ValueError("operation belongs to another trip")
                    return json.loads(previous[1])
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
            now = datetime.now(UTC).isoformat()
            value.setdefault("created_at", now)
            value["updated_at"] = now
            if (
                value.get("status") == "completed"
                and value.get("committed")
                and value.get("working") is None
            ):
                value["committed"].update(
                    revision=revision, created_at=value["created_at"], updated_at=now
                )
            if value.get("working"):
                value["working"].update(
                    revision=revision, created_at=value["created_at"], updated_at=now
                )
            summary = encode(index.workflow_summary(value))
            content = encode(value)
            if current:
                changed = conn.execute(
                    (
                        "UPDATE trip_plans SET revision=?,status=?,updated_at=?,summary_j"
                        "son=?,content_json=? WHERE id=? AND revision=?"
                    ),
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
            if operation_id:
                self._record_operation(conn, operation_id, value)
        self.trip_memory.repair(self)
        return value

    def _record_operation(self, conn, operation_id, value):
        from trip_assistant.storage.sqlite import encode

        conn.execute(
            "INSERT INTO operation_receipts VALUES (?,?,?)",
            (operation_id, value["id"], encode(value)),
        )

    def lookup_operation(self, operation_id):
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT result_json FROM operation_receipts WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def save_working(self, working, *, expected_revision, operation_id):
        previous = self.load(working["id"])
        value = deepcopy(working)
        value.pop("working", None)
        value["committed"] = (previous or {}).get("committed") or (
            previous if previous and previous.get("status") == "completed" else None
        )
        value["working"] = {
            k: deepcopy(v) for k, v in working.items() if k not in {"working", "committed"}
        }
        value["status"] = "running"
        return self.save(value, expected_revision=expected_revision, operation_id=operation_id)

    def commit(self, working, *, expected_revision, operation_id):
        if not working.get("validation", {}).get("valid"):
            raise ValueError("cannot commit an unvalidated trip")
        value = {k: deepcopy(v) for k, v in working.items() if k not in {"working", "committed"}}
        value["status"] = "completed"
        value["committed"] = deepcopy(value)
        value["working"] = None
        return self.save(value, expected_revision=expected_revision, operation_id=operation_id)

    def list_summaries(self):
        return self.index.list()

    def discovery_summaries(self):
        """Expose legacy history through stable storage IDs without making it executable."""
        from trip_assistant.storage.sqlite import metadata

        rows = self.list_summaries()
        with self.storage.connection() as conn:
            cutoff = metadata(conn, "history_cleared_before", "")
            for identity, updated, payload in conn.execute(
                "SELECT id,updated_at,payload_json FROM travel_history WHERE updated_at>?",
                (cutoff,),
            ):
                value = json.loads(payload)
                if conn.execute(
                    "SELECT 1 FROM trip_plans WHERE id=?", (value.get("workflow_id"),)
                ).fetchone():
                    continue
                rows.append(
                    {
                        "id": identity,
                        "status": "historical",
                        "read_only": True,
                        "summary": str(
                            value.get("summary") or value.get("destination") or "历史行程"
                        )[:320],
                        "updated_at": updated,
                        "confirmed_conditions": {
                            k: value.get(k) for k in ("start_date", "end_date") if value.get(k)
                        },
                        "tasks": [
                            {
                                "origin": value.get("origin"),
                                "destination": value.get("destination"),
                                "purpose": value.get("purpose"),
                                "conditions": {
                                    "departure_date": value.get("start_date"),
                                    "arrival_date": value.get("end_date"),
                                },
                            }
                        ],
                    }
                )
        return rows

    def read_history(self, identity):
        from trip_assistant.storage.sqlite import metadata

        storage_component(identity)
        with self.storage.connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM travel_history WHERE id=? AND updated_at>?",
                (identity, metadata(conn, "history_cleared_before", "")),
            ).fetchone()
        return (
            {**json.loads(row[0]), "id": identity, "historical": True, "read_only": True}
            if row
            else None
        )

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
                    "SELECT content_json FROM trip_plans WHERE status IN ('running','"
                    "needs_input','partial') ORDER BY updated_at DESC,id"
                )
            ]
        for value in values:
            self._validate(value, value.get("id"))
        from trip_assistant.trips.state import STOPPED, normalize_workflow_states

        values = [normalize_workflow_states(v) for v in values]
        values = [v for v in values if v.get("stop_reason") not in STOPPED]
        return values

    def _write_history(self, conn, record, *, importing=False, import_index=None):
        from trip_assistant.storage.migration import history_identity
        from trip_assistant.storage.sqlite import encode

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
        now = datetime.now(UTC).isoformat()
        if not importing:
            row.setdefault("trip_id", record_id)
            row.setdefault("timestamp", now)
        conn.execute(
            (
                "INSERT INTO travel_history VALUES (?,?,?,?) ON CONFLICT(id) DO U"
                "PDATE SET revision=excluded.revision,updated_at=excluded.updated"
                "_at,payload_json=excluded.payload_json"
            ),
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
        from trip_assistant.storage.sqlite import metadata, set_metadata

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
        from trip_assistant.storage.sqlite import metadata

        with self.storage.connection() as conn:
            conn.execute("BEGIN")
            cutoff = metadata(conn, "history_cleared_before", "")
            entries = []
            if include_workflows:
                for row in conn.execute(
                    (
                        "SELECT updated_at,content_json FROM trip_plans WHERE status='com"
                        "pleted' AND updated_at>?"
                    ),
                    (cutoff,),
                ):
                    value = json.loads(row[1])
                    self._validate(value, value.get("id"))
                    entries.append((row[0], self._history_view(value)))
            for row in conn.execute(
                (
                    "SELECT updated_at,payload_json FROM travel_history WHERE updated"
                    "_at>? ORDER BY updated_at,id"
                ),
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
        from trip_assistant.storage.sqlite import set_metadata

        with self.storage.transaction() as conn:
            set_metadata(conn, "history_cleared_before", datetime.now(UTC).isoformat())
