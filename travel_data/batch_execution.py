"""Isolated parallel fetches; parent state and evidence are merged serially."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

from agents.workflow_batches import mark_query_draft
from agents.workflow_contracts import task_by_id
from agents.workflow_state import STOPPED
from context.telemetry import model_scope
from travel_data.candidates import CandidateStore


async def execute_query_batch(batch, run, executor, *, after_tool=None):
    requests = batch["requests"]
    identities = [r["request_id"] for r in requests]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate request identities")
    if any(r.get("depends_on") for r in requests):
        remaining = list(requests)
        results, gaps = [], list(batch.get("gaps", []))
        records = (
            run.workflow.get("execution_plan", []) if run.workflow else run.query_execution_records
        )
        satisfied = {
            r["request_id"]
            for r in records
            if r.get("completed_at")
            and r.get("result_status") in {"ok", "partial"}
            and (
                not r.get("task_id")
                or (
                    run.workflow is not None
                    and task_by_id(run.workflow, r["task_id"])["revision"] == r.get("task_revision")
                )
            )
        }
        while remaining:
            ready = [r for r in remaining if set(r.get("depends_on", [])) <= satisfied]
            if not ready:
                gaps.extend(
                    {"request_id": r["request_id"], "reason": "dependency_unavailable"}
                    for r in remaining
                )
                break
            wave = await execute_query_batch(
                {"requests": [{k: v for k, v in r.items() if k != "depends_on"} for r in ready]},
                run,
                executor,
                after_tool=after_tool,
            )
            results.extend(wave["results"])
            gaps.extend(wave["gaps"])
            satisfied.update(
                r["request_id"] for r in wave["results"] if r["status"] in {"ok", "partial"}
            )
            remaining = [r for r in remaining if r not in ready]
        return {
            "results": sorted(results, key=lambda r: identities.index(r["request_id"])),
            "gaps": gaps,
        }
    if run.candidates is None:
        run.candidates = CandidateStore()
    plan = (
        run.workflow.setdefault("execution_plan", [])
        if run.workflow is not None
        else run.query_execution_records
    )
    started, gaps = [], list(batch.get("gaps", []))
    if run.workflow_tool_budget is None:
        run.workflow_tool_budget = max(0, run.limits.info_tool_calls - len(run.tool_requests))
    reservations = {}
    for request in batch["requests"]:
        from travel_data.tools import TOOL_DOMAINS

        request = {**request, "domain": request.get("domain") or TOOL_DOMAINS.get(request["name"])}
        if run.workflow and run.workflow.get("stop_reason") in STOPPED:
            gaps.append({"request_id": request["request_id"], "reason": "stopped"})
            continue
        prior = next((r for r in plan if r["request_id"] == request["request_id"]), None)
        if prior and prior.get("started_at"):
            gaps.append({"request_id": request["request_id"], "reason": "already_started"})
            continue
        reservation = 3 if request["name"] in {"train_search", "train_search_by_arrival"} else 1
        available = (
            run.workflow_tool_budget
            if run.workflow_tool_budget is not None
            else run.limits.info_tool_calls - len(run.tool_requests)
        )
        if request.get("task_id"):
            available = min(
                available,
                10
                - run.external_requests_by_task.get(request["task_id"], 0)
                - reservations.get(request["task_id"], 0),
            )
        if available < 1:
            gaps.append({"request_id": request["request_id"], "reason": "tool_budget"})
            continue
        reservation = min(reservation, available)
        if request.get("task_id"):
            reservations[request["task_id"]] = reservations.get(request["task_id"], 0) + reservation
        if run.workflow_tool_budget is not None:
            run.workflow_tool_budget -= reservation
        record = {
            **deepcopy(request),
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": None,
        }
        plan.append(record)
        private = replace(
            run,
            workflow=deepcopy(run.workflow)
            if request.get("task_id") and request["domain"] in {"train", "hotel"}
            else None,
            current_task_id=request.get("task_id"),
            domain_results={},
            query_records={},
            tool_requests=[],
            external_requests_by_task=deepcopy(run.external_requests_by_task),
            external_request_count=0,
            travel_conditions=deepcopy(run.travel_conditions),
            workflow_tool_budget=reservation - 1,
        )
        started.append((request, record, private, reservation))
    # Persist the started identities BEFORE any provider request.
    if after_tool and started:
        await after_tool()

    async def fetch(index, entry):
        request, _, private, _ = entry
        with model_scope(
            turn_id=run.turn_id,
            task_id=request.get("task_id"),
            task_revision=request.get("task_revision"),
        ):
            result = await executor.execute(
                request["name"], request["parameters"], private, call_id=request["request_id"]
            )
            return index, result

    pending = [asyncio.create_task(fetch(i, e)) for i, e in enumerate(started)]
    merged = []
    try:
        for completed in asyncio.as_completed(pending):
            index, result = await completed
            request, record, private, reservation = started[index]
            if run.workflow is not None:
                record = next(
                    r
                    for r in run.workflow["execution_plan"]
                    if r["request_id"] == request["request_id"]
                )
            result = {**result, "request_id": request["request_id"], "domain": request["domain"]}
            if run.workflow_tool_budget is not None:
                run.workflow_tool_budget += reservation - private.external_request_count
            run.tool_requests.extend(private.tool_requests)
            run.external_request_count += private.external_request_count
            run.external_requests_started |= private.external_requests_started
            record["completed_at"] = datetime.now(UTC).isoformat()
            record["result_ref"] = result.get("query_id")
            record["result_status"] = result["status"]
            if request.get("task_id"):
                task = task_by_id(run.workflow, request["task_id"])
                if task["revision"] != request["task_revision"]:
                    gaps.append({"request_id": request["request_id"], "reason": "stale_result"})
                    continue
                private_task = (
                    task_by_id(private.workflow, task["id"]) if private.workflow else task
                )
                for field in ("departure_date",):
                    if task.get("field_sources", {}).get(field) in {
                        "default",
                        "proposal",
                        "derived",
                    } and private_task["conditions"].get(field) != task["conditions"].get(field):
                        task["conditions"][field] = private_task["conditions"][field]
                        task["field_sources"][field] = private_task["field_sources"][field]
                run.external_requests_by_task[task["id"]] = (
                    run.external_requests_by_task.get(task["id"], 0)
                    + private.external_request_count
                )
                for identity, query in private.query_records.items():
                    run.workflow["results_by_query"][identity] = query
                    if identity not in task["query_ids"]:
                        task["query_ids"].append(identity)
                    run.query_records[identity] = deepcopy(query)
                container = task["query_containers"].get(request["domain"])
                if container is not None:
                    container.update(
                        status="ready" if result["status"] in {"ok", "partial"} else "failed",
                        parameters=deepcopy(result.get("query", request["parameters"])),
                        current_query_ref=result.get("query_id"),
                    )
                mark_query_draft(run.workflow, task["id"])
                if request["domain"] == "weather":
                    run.workflow.setdefault("auxiliary_results", {})[request["request_id"]] = (
                        deepcopy(result)
                    )
            else:
                run.domain_results[request["domain"]] = deepcopy(result)
            merged.append((index, result))
            if after_tool:
                await after_tool()
    finally:
        for pending_task in pending:
            if not pending_task.done():
                pending_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    return {"results": [r for _, r in sorted(merged)], "gaps": gaps}
