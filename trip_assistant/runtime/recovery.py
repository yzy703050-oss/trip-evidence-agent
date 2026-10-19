"""Reconcile persisted operations, never replay an unknown external side effect."""

from time import monotonic

from trip_assistant.presentation.trip_reply import bind_trip_reply
from trip_assistant.runtime.actions import import_legacy, native_actions
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.runtime.state import ExecutionState
from trip_assistant.runtime.trip_jobs import restore_trip_jobs
from trip_assistant.storage.actions import ActionStore


def restore_turn(sessions, turn_id, *, settings, clock=monotonic, trip_service=None):
    active = sessions.read_state().get("active_turn", {})
    if active.get("turn_id") != turn_id:
        raise ValueError("unknown active turn")
    legacy_executions = active.get("executions") or {}
    import_legacy(sessions, active)
    store = ActionStore(sessions)
    unknown = []
    for call in sessions.pending_tool_calls():
        if call["turn_id"] != turn_id:
            continue
        identity = call["id"]
        operation = turn_id + ":" + identity
        action = store.get(operation)
        execution = legacy_executions.get(identity, {})
        receipt = sessions.lookup_operation(operation)
        if action and action["state"] == "queued" and not receipt:
            continue
        if action and action["state"] == "unknown":
            unknown.append(identity)
            continue
        if execution.get("state") == "completed":
            result = execution["result"]
        elif receipt and call["name"] == "write_trip" and trip_service is not None:
            result = {"call_id": identity, "name": call["name"], **trip_service._result(receipt)}
            if call["arguments"].get("reply") is not None:
                result = bind_trip_reply(result, call["arguments"]["reply"])
        elif receipt and call["name"] == "query" and trip_service is not None:
            # Historical native queries remain native observations; never re-dispatch them.
            trip = trip_service._result(receipt)
            independent = receipt.get("query_observation", {}).get("data", [])
            data = [*independent, *trip["candidates"]]
            result = {
                "call_id": identity,
                "name": "query",
                "status": "ok"
                if data and all(v.get("status") == "ok" for v in data)
                else "partial",
                "data": data,
                "trip": trip,
                "trip_plan_id": trip["trip_plan_id"],
                "revision": trip["revision"],
                "independent_count": len(independent),
            }
        elif receipt and call["name"] == "write_memory":
            result = {
                "call_id": identity,
                "name": call["name"],
                "status": "ok" if not receipt["rejected"] else "partial",
                "data": receipt,
            }
        else:
            unknown.append(identity)
            result = {
                "call_id": identity,
                "name": call["name"],
                "status": "unknown",
                "reason": "上次调用结果未知，已保留进度，本次未重放该操作。",
                "error": {"code": "UNKNOWN_OPERATION", "details": {"operation_id": operation}},
            }
        sessions.record_result(identity, result, turn_id)
        if not action:
            native_actions(sessions, turn_id, [{k: call[k] for k in ("id", "name", "arguments")}])
        store.finish(
            operation,
            "unknown" if result.get("status") == "unknown" else "completed",
            "result:" + identity,
        )
    # Pairing may be durable even if the following action completion was interrupted.
    results_by_call = {
        e["tool_call_id"]: e["content"]
        for e in sessions.read_events(turn_id=turn_id, event_type="tool_result")
    }
    for action in store.list(turn_id, origin="model"):
        identity = action["payload"]["id"]
        if action["state"] == "unknown":
            unknown.append(identity)
        elif action["state"] in {"queued", "running"} and identity in results_by_call:
            store.finish(action["operation_id"], "completed", "result:" + identity)
    if trip_service is not None:
        unknown.extend(restore_trip_jobs(sessions, active, trip_service))
    stopped = active.get("status") in {"paused", "stopped", "completed"} or active.get(
        "stop_reason"
    ) in {"cancelled", "paused"}
    if unknown:
        active.update(
            status="needs_input",
            stop_reason="unknown_operation",
            reason="上次调用结果未知，已保留进度，本次无法继续该操作。",
        )
    machine = ExecutionState(active.get("execution_state"))
    events = sessions.read_events(turn_id=turn_id)
    results = [
        e["content"]
        for e in events
        if e["type"] == "tool_result"
        or e["type"] == "execution_job"
        and e.get("state") in {"completed", "unknown"}
    ]
    machine.learn(results)
    final_recorded = any(
        e["type"] == "message" and e.get("role") == "assistant" and e.get("final") for e in events
    )
    if final_recorded:
        final_event = next(
            e
            for e in reversed(events)
            if e["type"] == "message" and e.get("role") == "assistant" and e.get("final")
        )
        for action in store.list(turn_id, origin="system", states={"queued", "running"}):
            if action["kind"] == "reply":
                recorded_refs = final_event.get(
                    "trip_plan_refs",
                    [{"id": final_event["trip_plan_id"], "revision": final_event["trip_revision"]}]
                    if final_event.get("trip_plan_id") and final_event.get("trip_revision")
                    else [],
                )
                same_refs = action["payload"].get("trip_plans", []) == recorded_refs
                store.finish(
                    action["operation_id"],
                    "completed"
                    if same_refs and action["payload"]["answer"] == final_event["content"]
                    else "rejected",
                    final_event["event_id"],
                )
    if final_recorded and machine.phase not in {"completed", "stopped", "paused"}:
        # A final message is already durable; never ask the model to regenerate it.
        machine.phase = "reply_ready"
        machine.transition("stopped" if machine.stop_reason or unknown else "completed")
        active["status"] = machine.phase
    if not machine.summary_only:
        # Reconcile a crash between a paired result and its phase checkpoint.
        latest = next((e for e in reversed(events) if e["type"] == "tool_call"), {})
        call_ids = {c["id"] for c in latest.get("tool_calls", [])}
        batch = [
            r
            for r in results
            if r.get("call_id") in call_ids or r.get("parent_call_id") in call_ids
        ]
        machine.phase = "validating"
        machine.observe(batch)
        if unknown:
            machine.stop("unknown_operation")
    active["execution_state"] = machine.checkpoint()
    stopped = stopped or machine.phase in {"completed", "stopped", "paused"}
    sessions.update_state({"active_turn": active})
    return {
        "context": {**active, "turn_id": turn_id},
        "budget": TurnBudget(settings, snapshot=active, clock=clock),
        "can_resume": not unknown and not stopped,
        "unknown_calls": unknown,
        "reason": active.get("reason"),
    }
