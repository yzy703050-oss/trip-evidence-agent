"""Deterministic evidence maintenance; never propose or replay business selections."""

from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.runtime.limits import LimitError
from trip_assistant.runtime.trip_jobs import enqueue_trip_query, refresh_operation, run_trip_jobs
from trip_assistant.storage.actions import ActionStore
from trip_assistant.trips.conditions import task_by_id
from trip_assistant.trips.evidence import resolve_selection


def selection_refresh(executor, args, failure, turn):
    plan = args["tripplan"]
    if args["request_kind"] != "planning" or plan.get("operation") != "select":
        return None
    if turn["budget"].rounds >= turn["budget"].limits.planning_rounds:
        return None
    if any(
        plan.get(k)
        for k in ("confirmed_conditions", "field_sources", "condition_strengths", "route_tasks")
    ):
        return None
    if any(
        any(
            row.get(k)
            for k in (
                "conditions",
                "items",
                "origin",
                "destination",
                "field_sources",
                "condition_strengths",
            )
        )
        for row in plan.get("tasks", [])
    ):
        return None
    saved, _ = executor.scope(plan["trip_plan_id"], plan["expected_revision"])
    if turn.get("actions") and turn["actions"].get(refresh_operation(turn, saved["id"])):
        raise ToolFailure("AUTOMATIC_REFRESH_LIMIT", "本轮已更新过候选，未再次自动刷新。")
    selected = []
    for row in plan.get("tasks", []):
        task = task_by_id(saved, row["task_id"])
        if row.get("expected_revision", task["revision"]) != task["revision"]:
            raise ValueError("task revision changed")
        for domain, ref in row.get("selection_refs", {}).items():
            resolve_selection(saved, task["id"], task["revision"], ref)
            binding = saved["results_by_query"][ref["query_id"]]
            shown = turn.get("shown_candidates", {}).get(ref["candidate_id"], {})
            if binding["domain"] != domain or (
                ref["candidate_id"] not in binding.get("shown_candidate_ids", [])
                and (shown.get("query_id"), shown.get("result_revision"))
                != (ref["query_id"], ref["result_revision"])
            ):
                raise ValueError("selection is outside displayed task evidence")
            selected.append(ref["query_id"])
    query_id = failure["details"].get("query_id")
    if query_id not in selected:
        return None
    return executor.prepare_refresh(
        saved["id"], saved["revision"], [query_id, *executor.expired_queries(saved)]
    )


def refresh_observation(sessions, turn, call, plan, failure):
    sessions.checkpoint_turn(turn["turn_id"], turn["budget"].checkpoint())
    sessions.append(
        {
            "event_id": "expiry:" + turn["turn_id"] + ":" + call["id"],
            "turn_id": turn["turn_id"],
            "scope": "audit",
            "type": "execution_check",
            "content": {"call_id": call["id"], "error": failure},
        }
    )
    return {
        "call_id": call["id"],
        "name": call["name"],
        "status": "partial",
        "operation": "select",
        "trip_plan_id": plan["trip_plan_id"],
        "revision": plan["expected_revision"],
        "selection_saved": False,
        "reason": "本次选择尚未保存，候选更新后需要重新选择。",
        "automatic_refresh": plan,
    }


def failed_refresh_requires_summary(review, results):
    trip = review.get("tripplan", {})
    if not trip.get("trip_plan_id") or any(v.get("items") for v in trip.get("candidates", [])):
        return False
    return any(
        r.get("kind") == "execution_job"
        and r.get("invalidated_evidence")
        and r.get("trip_plan_id") == trip["trip_plan_id"]
        and r.get("revision") == trip["revision"]
        and r.get("status") in {"partial", "error"}
        for r in results
    )


async def refresh_before_review(executor, trip_ids, turn, sessions, budget):
    if not executor or not budget.planning or budget.rounds >= budget.limits.planning_rounds:
        return []
    plans, keys = [], []
    for trip_id in sorted(trip_ids):
        if ActionStore(sessions).get(refresh_operation(turn, trip_id)):
            continue
        saved = executor.service.store.load(trip_id)
        if not saved or saved.get("stop_reason") in {"paused", "cancelled"}:
            continue
        expired = executor.expired_queries(saved)
        if expired:
            plan = executor.prepare_refresh(trip_id, saved["revision"], expired)
            keys.extend(
                executor.service.queries.expand_queries(
                    {"query_plan": [r["item"] for r in plan["requests"]]}, turn
                )
            )
            plans.append(plan)
    if not plans:
        return []
    try:
        budget.check_external_queries(keys)
    except LimitError:
        # Selection preflight will surface the same quota without any external request.
        return []
    for plan in plans:
        enqueue_trip_query(sessions, turn, None, {"automatic_refresh": plan})
    return await run_trip_jobs(executor, turn, sessions)
