"""Program-owned hard requirements, separate from the model's adjustable proposal."""

from copy import deepcopy

from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.trips.strengths import condition_is_hard, equivalent, global_is_hard


def _conditions(values, is_hard):
    result = {k: deepcopy(v) for k, v in values.items() if k != "constraints" and is_hard(k)}
    limits = {k: deepcopy(v) for k, v in values.get("constraints", {}).items() if is_hard(k)}
    if limits:
        result["constraints"] = limits
    return result


def compile_constraints(work, previous=None):
    """Compile structured requirements without inferring intent from user prose."""
    snapshot = {
        "global": {
            "conditions": _conditions(
                work.get("confirmed_conditions", {}), lambda k: global_is_hard(work, k)
            ),
        },
        "tasks": {
            task["id"]: {
                "route": {
                    "origin": task.get("origin"),
                    "destination": task["destination"],
                    "purpose": task.get("purpose"),
                },
                "conditions": _conditions(
                    task.get("conditions", {}),
                    lambda k, task=task: condition_is_hard(work, task, k)
                    or (
                        k in task.get("conditions", {}).get("constraints", {})
                        and task.get("condition_strengths", {}).get(
                            k, work.get("condition_strengths", {}).get(k, "hard")
                        )
                        == "hard"
                    ),
                ),
                "activities": [
                    deepcopy(a) for a in task.get("content", []) if a.get("strength") == "hard"
                ],
            }
            for task in work["tasks"]
        },
        "policy": {
            k: deepcopy(v)
            for k, v in (work.get("policy_context") or {}).items()
            if k
            in {
                "status",
                "constraints",
                "applicability",
                "references",
                "effective_date",
                "missing_fields",
                "conflicts",
            }
        },
    }
    old = (previous or {}).get("constraint_snapshot")
    revision = old.get("revision", 1) if old else 1
    if old and not equivalent(snapshot, {k: v for k, v in old.items() if k != "revision"}):
        revision += 1
    return {"revision": revision, **snapshot}


def bind_constraints(work, previous, *, verifier=False):
    snapshot = compile_constraints(work, previous)
    if previous and verifier:
        old = previous.get("constraint_snapshot") or compile_constraints(previous)
        if not equivalent(snapshot["global"], old["global"]) or not equivalent(
            snapshot["tasks"], old["tasks"]
        ):
            raise ToolFailure(
                "REPLAN_REQUIRED",
                "局部评估不能改变既有硬条件、硬活动或路线；保留原需求修复，"
                "需要整体规划时先 request_replan。交接不代表用户授权放宽硬要求。",
                {"trip_plan_id": previous["id"], "constraint_revision": old["revision"]},
            )
    work["constraint_snapshot"] = snapshot
