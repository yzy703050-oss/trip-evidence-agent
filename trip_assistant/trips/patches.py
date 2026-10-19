"""Remove proven repeated values without losing explicit patch semantics."""

from copy import deepcopy

from trip_assistant.storage.trips import WorkflowConflictError
from trip_assistant.trips.strengths import condition_is_hard, equivalent


def normalize_trip_patch(saved, patch):
    if saved["revision"] != patch.get("expected_revision"):
        raise WorkflowConflictError("trip revision changed")
    result = deepcopy(patch)
    if patch.get("operation") != "amend":
        return result

    def clean(target, current):
        for key, value in list(target.items()):
            if key in {
                "conditions",
                "confirmed_conditions",
                "field_sources",
                "condition_strengths",
            }:
                originally_nonempty = bool(value)
                for field, proposed in list(value.items()):
                    # Empty containers/null may be a deliberate clear; never erase them.
                    if (
                        proposed is not None
                        and proposed != []
                        and proposed != {}
                        and (
                            equivalent((current.get(key) or {}).get(field), proposed, field)
                            or (
                                key == "condition_strengths"
                                and current is not saved
                                and field not in current.get("condition_strengths", {})
                                and field in current.get("conditions", {})
                                and proposed == "soft"
                                and not condition_is_hard(saved, current, field)
                            )
                        )
                    ):
                        value.pop(field)
                if originally_nonempty and not value:
                    target.pop(key)
            elif key in {"origin", "destination", "purpose", "requires_hotel"}:
                if value is not None and current.get(key) == value:
                    target.pop(key)

    clean(result, saved)
    by_id = {t["id"]: t for t in saved["tasks"]}
    rows = []
    for row in result.get("tasks", []):
        task = by_id.get(row.get("task_id"))
        if task is None:
            raise ValueError("trip task not found")
        if row.get("expected_revision", task.get("revision")) != task.get("revision"):
            raise WorkflowConflictError("task revision changed")
        clean(row, task)
        if set(row) - {"task_id", "expected_revision", "components"}:
            rows.append(row)
    if "tasks" in result:
        result["tasks"] = rows
    return result
