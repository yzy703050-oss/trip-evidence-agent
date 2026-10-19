"""Query small database summaries before loading the selected workflow."""

import json
import logging
from copy import deepcopy


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
            "session_id",
            "user_confirmed",
            "stop_reason",
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
    value["data_mode"] = (
        "simulation"
        if any(
            str((r.get("source") or {}).get("provider", "")).startswith("simulation:")
            for r in workflow.get("results_by_query", {}).values()
        )
        else "provider"
    )
    value["policy_available"] = bool(workflow.get("policy_context"))
    value["analysis_eligible"] = workflow.get("status") == "completed" and bool(
        (workflow.get("validation") or {}).get("valid")
    )
    value["selection_source"] = (
        "user_adopted"
        if any(t.get("user_adopted") for t in workflow.get("tasks", []))
        else "assistant_proposal"
    )
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

    def list(self):
        with self.store.storage.connection() as conn:
            rows = list(
                conn.execute(
                    "SELECT id,revision,summary_json FROM trip_plans ORDER BY updated_at DESC,id"
                )
            )
        values = []
        try:
            for identity, revision, payload in rows:
                value = json.loads(payload)
                if (
                    not isinstance(value, dict)
                    or value.get("id") != identity
                    or type(value.get("revision")) is not int
                    or value["revision"] != revision
                    or not isinstance(value.get("tasks"), list)
                ):
                    raise ValueError("invalid workflow summary; explicit rebuild required")
                from trip_assistant.trips.state import normalize_workflow_states

                values.append(normalize_workflow_states(value, check_evidence=False))
        except ValueError:
            logging.getLogger(__name__).exception(
                "Invalid workflow summary; explicit rebuild required"
            )
            raise
        return values

    def rebuild(self):
        from trip_assistant.storage.sqlite import encode

        with self.store.storage.transaction() as conn:
            for row in conn.execute("SELECT id,content_json FROM trip_plans").fetchall():
                value = json.loads(row[1])
                self.store._validate(value, row[0])
                conn.execute(
                    "UPDATE trip_plans SET summary_json=? WHERE id=?",
                    (encode(workflow_summary(value)), row[0]),
                )
        return self.list()
