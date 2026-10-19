"""Durable TripPlan query jobs, distinct from model-authored native tool calls."""

import asyncio
from copy import deepcopy

from trip_assistant.runtime.actions import query_jobs
from trip_assistant.runtime.errors import error_payload
from trip_assistant.runtime.progress import report_progress
from trip_assistant.runtime.scheduler import gather_owned
from trip_assistant.runtime.telemetry import model_scope
from trip_assistant.storage.actions import ActionStore


def record_job(sessions, turn, job, state, result=None):
    store = ActionStore(sessions)
    if store.get(job["operation_id"]) is None:
        store.accept_many(
            turn["turn_id"],
            [
                {
                    "operation_id": job["operation_id"],
                    "origin": "system",
                    "kind": "trip_query",
                    "payload": {
                        k: deepcopy(v) for k, v in job.items() if k not in {"state", "result"}
                    },
                }
            ],
        )
    if state == "running" and not store.claim(job["operation_id"]):
        return False
    job["state"] = state
    if result is not None:
        job["result"] = {
            **result,
            "call_id": job["operation_id"],
            "parent_call_id": job["parent_call_id"],
        }
    sessions.append(
        {
            "event_id": f"job:{job['operation_id']}:{state}",
            "turn_id": turn["turn_id"],
            "scope": "session",
            "type": "execution_job",
            "state": state,
            "job_id": job["operation_id"],
            "content": job["result"] if result is not None else dict(job),
        }
    )
    if state in {"completed", "unknown"}:
        store.finish(job["operation_id"], state, f"job:{job['operation_id']}:{state}")
    if turn.get("budget"):
        sessions.checkpoint_turn(turn["turn_id"], turn["budget"].checkpoint())
    return True


def enqueue_trip_query(sessions, turn, call_id, result):
    refresh = result.get("automatic_refresh")
    if not refresh and (
        result.get("stop_planning")
        or not any(p["reason"] == "awaiting_query" for p in result.get("pending_queries", []))
    ):
        return
    operation = (
        refresh_operation(turn, refresh["trip_plan_id"])
        if refresh
        else f"{turn['turn_id']}:{call_id}:queries"
    )
    if ActionStore(sessions).get(operation) is None:
        job = {
            "operation_id": operation,
            "parent_call_id": call_id,
            "trip_plan_id": refresh["trip_plan_id"] if refresh else result["trip_plan_id"],
            "expected_revision": refresh["expected_revision"] if refresh else result["revision"],
            "state": "queued",
            **({"refresh": deepcopy(refresh)} if refresh else {}),
        }
        record_job(sessions, turn, job, "queued")


def refresh_operation(turn, trip_id):
    return f"{turn['turn_id']}:refresh:{trip_id}"


async def run_trip_jobs(executor, turn, sessions):
    jobs = [j for j in query_jobs(sessions, turn["turn_id"]).values() if j["state"] == "queued"]

    async def run(job):
        turn["budget"].require_time()
        if not record_job(sessions, turn, job, "running"):
            return None
        report_progress("正在按行程查询车次与住宿…")
        try:
            with model_scope(execution_job_id=job["operation_id"], job_type="trip_query"):
                async with asyncio.timeout(turn["budget"].remaining_seconds()):
                    value = (
                        await executor.refresh(
                            job["refresh"], {**turn, "operation_id": job["operation_id"]}
                        )
                        if job.get("refresh")
                        else await executor.run(
                            job["trip_plan_id"],
                            job["expected_revision"],
                            {**turn, "operation_id": job["operation_id"]},
                        )
                    )
        except Exception as error:
            value = {
                "name": "trip_query",
                "kind": "execution_job",
                "status": "error",
                "trip_plan_id": job["trip_plan_id"],
                "reason": str(error),
                "error": error_payload(error),
            }
        record_job(sessions, turn, job, "completed", value)
        return job["result"]

    groups = {}
    for job in jobs:
        groups.setdefault(job["trip_plan_id"], []).append(job)

    async def run_trip(group):
        return [value for job in group if (value := await run(job)) is not None]

    completed = await gather_owned(*(run_trip(group) for group in groups.values()))
    by_id = {value["call_id"]: value for group in completed for value in group}
    return [by_id[job["operation_id"]] for job in jobs if job["operation_id"] in by_id]


def restore_trip_jobs(sessions, active, service):
    """Rebuild queued work; a running request without a receipt is never replayed."""
    from trip_assistant.trips.query_execution import TripQueryExecutor

    turn = active
    events = sessions.read_events(turn_id=turn["turn_id"])
    calls = {c["id"]: c for e in events if e["type"] == "tool_call" for c in e["tool_calls"]}
    for event in events:
        if event["type"] != "tool_result":
            continue
        result = event["content"]
        call = calls.get(event["tool_call_id"], {})
        refresh = result.get("automatic_refresh")
        if result.get("name") == "write_trip" and (
            refresh
            or call.get("arguments", {}).get("tripplan", {}).get("operation", "propose")
            in {"propose", "amend"}
        ):
            operation = (
                refresh_operation(turn, refresh["trip_plan_id"])
                if refresh
                else f"{turn['turn_id']}:{event['tool_call_id']}:queries"
            )
            if ActionStore(sessions).get(operation) is None and result.get("trip_plan_id"):
                current = service.store.load(result["trip_plan_id"])
                if current and current["revision"] != result.get("revision"):
                    continue
            enqueue_trip_query(sessions, turn, event["tool_call_id"], result)
    executor = TripQueryExecutor(service)
    unknown = []
    for job in query_jobs(sessions, turn["turn_id"]).values():
        if job["state"] == "unknown":
            unknown.append(job["operation_id"])
            continue
        if job["state"] not in {"queued", "running"}:
            continue
        receipt = service.store.lookup_operation(job["operation_id"])
        if receipt:
            value = executor.result(receipt)
            if job.get("refresh"):
                value["invalidated_evidence"] = job["refresh"]["invalidated_evidence"]
            record_job(sessions, turn, job, "completed", value)
        elif job["state"] == "running":
            unknown.append(job["operation_id"])
            record_job(
                sessions,
                turn,
                job,
                "unknown",
                {
                    "name": "trip_query",
                    "kind": "execution_job",
                    "status": "unknown",
                    "trip_plan_id": job["trip_plan_id"],
                    "reason": "上次行程查询结果未知，已保留原方案与进度，未重放。",
                    "error": {
                        "code": "UNKNOWN_OPERATION",
                        "details": {"operation_id": job["operation_id"]},
                    },
                },
            )
    return unknown
