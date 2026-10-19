"""Shared durable action admission and recovery adapters."""

from trip_assistant.storage.actions import ActionStore


def recover_reply(sessions, turn_id, trip_service=None):
    from trip_assistant.runtime.state import ExecutionState

    events = sessions.read_events(turn_id=turn_id)
    calls = next((e["tool_calls"] for e in reversed(events) if e["type"] == "tool_call"), [])
    identities = {c["id"] for c in calls}
    results = [
        e["content"]
        for e in events
        if e["type"] == "tool_result"
        and e["tool_call_id"] in identities
        or e["type"] == "execution_job"
        and e.get("state") in {"completed", "unknown"}
        and e["content"].get("parent_call_id") in identities
    ]
    if trip_service:
        for result in results:
            if result.get("name") == "write_trip" and result.get("trip_plan_id"):
                current = trip_service.store.load(result["trip_plan_id"])
                if not current or current["revision"] != result.get("revision"):
                    return None
    return ExecutionState.ready_reply(calls, results)


def stage_reply(sessions, turn_id, answer, results):
    from hashlib import sha256

    from trip_assistant.presentation.facts import trip_results
    from trip_assistant.storage.sqlite import encode

    revisions = {}
    for result in trip_results(results):
        if result.get("trip_plan_id") and result.get("revision"):
            identity = result["trip_plan_id"]
            revisions[identity] = max(revisions.get(identity, 0), result["revision"])
    payload = {
        "answer": answer,
        "trip_plans": [
            {"id": identity, "revision": revision}
            for identity, revision in sorted(revisions.items())
        ],
    }
    operation = turn_id + ":reply:" + sha256(encode(payload).encode()).hexdigest()[:16]
    store = ActionStore(sessions)
    store.accept_many(
        turn_id,
        [{"operation_id": operation, "origin": "system", "kind": "reply", "payload": payload}],
    )
    store.claim(operation)
    return operation


def native_actions(sessions, turn_id, calls):
    return ActionStore(sessions).accept_many(
        turn_id,
        [
            {
                "operation_id": turn_id + ":" + c["id"],
                "origin": "model",
                "kind": c["name"],
                "batch_id": "call:" + ",".join(call["id"] for call in calls),
                "payload": c,
            }
            for c in calls
        ],
    )


def finish_native(sessions, turn_id, result):
    sessions.record_result(result["call_id"], result, turn_id)
    store = ActionStore(sessions)
    operation = turn_id + ":" + result["call_id"]
    action = store.get(operation)
    if action:
        state = result.get("status")
        store.finish(
            operation,
            state if state in {"unknown", "rejected"} else "completed",
            "result:" + result["call_id"],
        )


def query_jobs(sessions, turn_id):
    store = ActionStore(sessions)
    return {
        a["operation_id"]: {
            **a["payload"],
            "state": a["state"],
            **({"result": store.result(a)} if a["result_ref"] else {}),
        }
        for a in store.list(turn_id, origin="system")
        if a["kind"] == "trip_query"
    }


def import_legacy(sessions, active):
    """Read old snapshots once; the action ledger remains authoritative afterwards."""
    store = ActionStore(sessions)
    calls = {
        c["id"]: c
        for e in sessions.read_events(turn_id=active["turn_id"])
        if e["type"] == "tool_call"
        for c in e["tool_calls"]
    }
    for identity, old in (active.get("executions") or {}).items():
        operation = old.get("operation_id", active["turn_id"] + ":" + identity)
        if store.get(operation) or identity not in calls:
            continue
        native_actions(sessions, active["turn_id"], [calls[identity]])
        if old.get("state") == "started":
            store.claim(operation)
    # Jobs also have durable historical events; replay them through the same adapter.
    from trip_assistant.runtime.trip_jobs import record_job

    jobs = dict(active.get("trip_query_jobs") or {})
    for event in sessions.read_events(turn_id=active["turn_id"], event_type="execution_job"):
        identity, state = event["job_id"], event["state"]
        if state in {"queued", "running"}:
            jobs[identity] = {**event["content"], "state": state}
        elif identity in jobs:
            jobs[identity].update(state=state, result=event["content"])
    for job in jobs.values():
        if store.get(job["operation_id"]):
            continue
        state = job.get("state", "queued")
        record_job(sessions, active, job, "queued")
        if state == "running":
            record_job(sessions, active, job, "running")
        elif state in {"completed", "unknown"}:
            record_job(sessions, active, job, state, job.get("result", {}))
    active.pop("executions", None)
    active.pop("trip_query_jobs", None)
