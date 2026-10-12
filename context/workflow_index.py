"""Small, user-owned discovery index; canonical JSON remains the resume authority."""

from copy import deepcopy
import json
import logging

from context.session_store import atomic_json_write


def workflow_summary(workflow):
    value = {
        key: deepcopy(workflow.get(key))
        for key in (
            "id",
            "revision",
            "status",
            "original_query",
            "created_at",
            "updated_at",
            "confirmed_conditions",
        )
    }
    tasks = []
    for task in workflow.get("tasks", []):
        row = {
            key: deepcopy(task.get(key))
            for key in (
                "id",
                "revision",
                "status",
                "origin",
                "destination",
                "purpose",
                "requires_hotel",
                "depends_on",
            )
        }
        row["conditions"] = {
            k: deepcopy(v)
            for k, v in task.get("conditions", {}).items()
            if k
            in {
                "departure_date",
                "arrival_date",
                "arrival_before",
                "check_in",
                "check_out",
                "nights",
                "passengers",
                "guests",
                "flexible_dates",
                "hotel_quote_required",
                "hotel_keywords",
                "constraints",
            }
        }
        row["field_sources"] = {
            k: v for k, v in task.get("field_sources", {}).items() if k in row["conditions"]
        }
        row["summary"] = str(task.get("summary", ""))[:160]
        tasks.append(row)
    value["tasks"] = tasks
    value["task_count"] = len(tasks)
    value["summary"] = "；".join(t["summary"] for t in tasks if t["summary"])[:320]
    if not value["summary"]:
        value["summary"] = (
            " → ".join(
                [tasks[0].get("origin") or "起点待补"]
                + [t.get("destination") or "目的地待补" for t in tasks]
            )
            if tasks
            else ""
        )
    if workflow.get("checkpoint"):
        value["checkpoint"] = {
            k: deepcopy(v)
            for k, v in workflow["checkpoint"].items()
            if k
            in {
                "kind",
                "question",
                "resume_task_id",
                "affected_task_ids",
                "missing_fields",
                "expected_workflow_revision",
            }
        }
    return value


class WorkflowIndex:
    def __init__(self, store):
        self.store = store
        self.path = store.directory.parent / "workflow-index.json"
        self.lock_path = self.path.with_suffix(".lock")

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or "user_id" not in value:
            return None
        if value.get("user_id") != self.store.user_id:
            raise ValueError("workflow index owner mismatch")
        if (
            value.get("schema_version") != 1
            or not isinstance(value.get("entries"), dict)
            or not isinstance(value.get("files"), dict)
        ):
            return None
        if any(
            not isinstance(row, dict)
            or row.get("id") != key
            or type(row.get("revision")) is not int
            or not isinstance(row.get("tasks"), list)
            for key, row in value["entries"].items()
        ):
            return None
        return value

    def _files(self):
        return {
            p.stem: [p.stat().st_mtime_ns, p.stat().st_size]
            for p in self.store.directory.glob("*.json")
            if not p.name.startswith(".") and not p.stem.startswith("history_")
        }

    def _rebuild(self):
        for _ in range(3):
            before = self._files()
            rows = self.store.list_all()
            stamps = self._files()
            if before == stamps:
                break
        else:
            raise OSError("workflow files changed during summary rebuild; retry discovery")
        data = {
            "schema_version": 1,
            "user_id": self.store.user_id,
            "entries": {w["id"]: workflow_summary(w) for w in rows},
            "files": stamps,
        }
        atomic_json_write(self.path, data)
        return data

    def list(self):
        from context.workflow_store import _file_lock

        with _file_lock(self.lock_path):
            data = self._read()
            if data is None or data["files"] != self._files():
                logging.getLogger(__name__).info('Rebuilding missing, invalid or stale workflow summary index')
                data = self._rebuild()
            return sorted(
                deepcopy(list(data["entries"].values())),
                key=lambda w: w.get("updated_at") or "",
                reverse=True,
            )

    def update(self, workflow):
        from context.workflow_store import _file_lock

        with _file_lock(self.lock_path):
            data = self._read()
            if data is None:
                data = self._rebuild()
            old = data["entries"].get(workflow["id"])
            if old and old["revision"] > workflow["revision"]:
                return
            # A concurrent newer save must not acquire the older projection's file stamp.
            path = self.store._path(workflow["id"])
            with _file_lock(path.with_suffix(".lock")):
                observed = self.store._read(path)
                if observed["revision"] != workflow["revision"]:
                    workflow = observed
                data["entries"][workflow["id"]] = workflow_summary(workflow)
                stat = path.stat()
                data["files"][workflow["id"]] = [stat.st_mtime_ns, stat.st_size]
            atomic_json_write(self.path, data)
