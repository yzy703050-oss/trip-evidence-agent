"""Evidence execution for persisted TripPlan items; invoked by the execution state machine."""

import asyncio
from copy import deepcopy

from trip_assistant.providers.views import remember_view
from trip_assistant.runtime.errors import ToolFailure
from trip_assistant.runtime.telemetry import model_scope
from trip_assistant.trips.conditions import task_by_id
from trip_assistant.trips.contracts import QueryItem
from trip_assistant.trips.evidence import evidence_is_fresh


class TripQueryExecutor:
    def __init__(self, service):
        self.service = service

    def expired_queries(self, saved):
        return [
            ref
            for task in saved["tasks"]
            for container in task.get("query_containers", {}).values()
            if container.get("status") == "ready"
            and (ref := container.get("current_query_ref"))
            and (row := saved.get("results_by_query", {}).get(ref))
            and row.get("task_revision") == task["revision"]
            and not evidence_is_fresh(row, self.service.scheduler.settings.evidence_max_age_seconds)
        ]

    def prepare_refresh(self, trip_id, revision, query_ids):
        saved, _ = self.scope(trip_id, revision)
        refs, items = [], []
        for query_id in dict.fromkeys(query_ids):
            row = saved.get("results_by_query", {}).get(query_id)
            if not row:
                raise ValueError("refresh query is not bound to this TripPlan")
            task = task_by_id(saved, row["task_id"])
            container = task.get("query_containers", {}).get(row["domain"], {})
            if row.get("task_revision") != task["revision"] or (
                container.get("status") != "ready" or container.get("current_query_ref") != query_id
            ):
                raise ValueError("refresh cannot restore an invalid task binding")
            item = QueryItem.model_validate(
                {
                    "domain": row["domain"],
                    "parameters": row["parameters"],
                    "constraints": row.get("constraints", {}),
                    "ranking": row.get("ranking", []),
                    "refresh": True,
                }
            ).model_dump(exclude_none=True)
            refs.append(
                {
                    "trip_plan_id": trip_id,
                    "task_id": task["id"],
                    "domain": row["domain"],
                    "query_id": query_id,
                    "result_revision": row["result_revision"],
                }
            )
            items.append({"task_id": task["id"], "item": item})
        if not refs:
            raise ValueError("refresh needs bound evidence")
        return {
            "trip_plan_id": trip_id,
            "expected_revision": revision,
            "invalidated_evidence": refs,
            "requests": items,
        }

    async def refresh(self, plan, turn):
        operation = turn["operation_id"]
        async with self.service.scheduler.write_lock(plan["trip_plan_id"]):
            receipt = self.service.store.lookup_operation(operation)
            if not receipt:
                turn["budget"].require_time()
                turn["budget"].check_external_queries(
                    self.service.queries.expand_queries(
                        {"query_plan": [r["item"] for r in plan["requests"]]}, turn
                    )
                )
                current = self.prepare_refresh(
                    plan["trip_plan_id"],
                    plan["expected_revision"],
                    [r["query_id"] for r in plan["invalidated_evidence"]],
                )
                if current != plan:
                    raise ValueError("refresh evidence version changed")
                work, _ = self.scope(plan["trip_plan_id"], plan["expected_revision"])
                for ref in plan["invalidated_evidence"]:
                    work["results_by_query"][ref["query_id"]]["needs_revalidation"] = True
                stale = {r["query_id"] for r in plan["invalidated_evidence"]}
                work["last_candidate_views"] = [
                    v
                    for v in work.get("last_candidate_views", [])
                    if v.get("query_id") not in stale
                ]
                requests = []
                for request in plan["requests"]:
                    item = deepcopy(request["item"])
                    ref = next(
                        r
                        for r in plan["invalidated_evidence"]
                        if r["task_id"] == request["task_id"] and r["domain"] == item["domain"]
                    )
                    original = work["results_by_query"][ref["query_id"]].get("evidence_ref", {})
                    cached = self.service.queries.evidence.find_valid(
                        self.service.queries.request_key(item),
                        now=self.service.queries.now(),
                        max_age=self.service.scheduler.settings.evidence_max_age_seconds,
                    )
                    if (
                        cached
                        and original.get("query_id") == cached["query_id"]
                        and (cached["result_revision"] > original["result_revision"])
                    ):
                        item["refresh"] = False
                    requests.append((request["task_id"], item))
                work.setdefault("pending_queries", []).extend(
                    {"task_id": task_id, "item": deepcopy(item), "reason": "awaiting_query"}
                    for task_id, item in requests
                )
                await self.query_trip(work, requests, turn)
                receipt = self.service.store.lookup_operation(operation)
            return {**self.result(receipt), "invalidated_evidence": plan["invalidated_evidence"]}

    def scope(self, trip_id, revision):
        work = self.service.store.load(trip_id)
        if not work:
            raise ToolFailure("UNKNOWN_TRIP_REFERENCE", "TripPlan not found")
        if work["revision"] != revision:
            raise ToolFailure("TRIP_REVISION_CHANGED", "TripPlan query revision changed")
        if work.get("stop_reason") in {"paused", "cancelled"}:
            raise ToolFailure("TRIP_STOPPED", "stopped TripPlan must be resumed through write_trip")
        requests = [
            (p["task_id"], p["item"])
            for p in work.get("pending_queries", [])
            if p["reason"] == "awaiting_query"
        ]
        return work, requests

    def expand_queries(self, args, turn):
        if args["tripplan"].get("operation", "propose") not in {"propose", "amend"}:
            return []
        return self.service.expand_queries(args, turn)

    async def run(self, trip_id, revision, turn):
        receipt = self.service.store.lookup_operation(turn["operation_id"])
        if receipt:
            return self.result(receipt)
        async with self.service.scheduler.write_lock(trip_id):
            receipt = self.service.store.lookup_operation(turn["operation_id"])
            if receipt:
                return self.result(receipt)
            work, requests = self.scope(trip_id, revision)
            if not requests:
                return self.result(work)
            await self.query_trip(work, requests, turn)
            return self.result(self.service.store.lookup_operation(turn["operation_id"]))

    def result(self, saved):
        trip = self.service._result(saved)
        data = deepcopy(trip["candidates"])
        if "_query_targets" in saved:
            targets = {(r["task_id"], r["domain"]) for r in saved["_query_targets"]}
            data = [v for v in data if (v.get("task_id"), v.get("domain")) in targets]
        return {
            "name": "trip_query",
            "kind": "execution_job",
            "status": "ok" if data and all(v.get("status") == "ok" for v in data) else "partial",
            "data": data,
            "trip_plan_id": trip["trip_plan_id"],
            "revision": trip["revision"],
            "trip": trip,
            "stop_planning": trip["stop_planning"],
            "stop_reason": trip["stop_reason"],
        }

    async def query_trip(self, saved, requests, turn):
        operation_id = turn["operation_id"]
        receipt = self.service.store.lookup_operation(operation_id)
        if receipt:
            return self.service._result(receipt)
        work = deepcopy(saved)
        work["_query_targets"] = [
            {"task_id": task_id, "domain": item["domain"]} for task_id, item in requests
        ]
        work.pop("working", None)
        work.pop("committed", None)
        expected = work["revision"]
        args = {"operation": "query"}
        work.pop("query_observation", None)
        remaining = [p for p in work["pending_queries"] if p["reason"] != "awaiting_query"]

        async def fetch(task_id, item):
            with model_scope(trip_plan_id=work["id"], task_id=task_id):
                return await self.service.queries.fetch(item, turn)

        if requests:
            # Keep the model's proposal before network work, without a success receipt.
            work.update(
                stop_reason="interrupted_query",
                stop_explanation="查询执行中；若中断，保留参数与进度待核实。",
            )
            for pending in work["pending_queries"]:
                if pending["reason"] == "awaiting_query":
                    pending["reason"] = "query_in_progress"
            saved = self.service.store.save_working(
                work, expected_revision=expected, operation_id=None
            )
            expected = saved["revision"]
            work["revision"] = expected
        views = await asyncio.gather(*(fetch(task_id, item) for task_id, item in requests))
        work["pending_queries"] = remaining
        for (task_id, item), view in zip(requests, views, strict=False):
            view["task_id"] = task_id
            task = task_by_id(work, task_id)
            if item["domain"] not in {"train", "hotel"}:
                continue
            query_id = view.get("query_id")
            if view.get("status") not in {"ok", "partial"}:
                # Preserve historical facts, but failed refresh cannot certify the old quote.
                for query_id in task.get("query_ids", []):
                    old = work["results_by_query"][query_id]
                    if old["domain"] == item["domain"]:
                        old["needs_revalidation"] = True
                container = task["query_containers"][item["domain"]]
                container.update(
                    status="failed",
                    failure_reason=view.get("reason") or view.get("message") or view["status"],
                )
                work["pending_queries"].append(
                    {"task_id": task_id, "item": deepcopy(item), "reason": "query_failed"}
                )
                continue
            if not query_id:
                continue
            raw = self.service.queries.evidence.get(query_id, view["result_revision"])
            # Binding ID is task-owned; external evidence identity remains separately recorded.
            binding = f"{task_id}:{query_id}"
            row = {
                **deepcopy(raw),
                "id": binding,
                "task_id": task_id,
                "task_revision": task["revision"],
                "parameters": deepcopy(item["parameters"]),
                "constraints": item["constraints"],
                "ranking": item.get("ranking", []),
                "evidence_ref": {
                    "query_id": query_id,
                    "result_revision": view["result_revision"],
                },
                "shown_candidate_ids": sorted(
                    {c["id"] for c in view["items"] if c.get("id")}
                    | {
                        key
                        for key, seen in turn.get("shown_candidates", {}).items()
                        if seen.get("query_id") == query_id
                        and seen.get("result_revision") == view["result_revision"]
                    }
                ),
                "needs_revalidation": False,
            }
            work["results_by_query"][binding] = row
            if binding not in task["query_ids"]:
                task["query_ids"].append(binding)
            task.setdefault("query_containers", {})[item["domain"]] = {
                "status": "ready" if view["status"] in {"ok", "partial"} else "failed",
                "current_query_ref": binding,
                "history_refs": [],
            }
            view["query_id"] = binding
            remember_view(turn, view)
            for candidate in view["items"]:
                if candidate.get("id"):
                    turn.setdefault("shown_candidates", {})[candidate["id"]] = {
                        "query_id": binding,
                        "result_revision": view["result_revision"],
                    }
            task.update(
                status="draft",
                draft_stage="query" if not task.get("draft_plan") else "selection",
            )
        refreshed = {(task_id, item["domain"]) for task_id, item in requests}
        kept = [
            view
            for view in work.get("last_candidate_views", [])
            if (row := work.get("results_by_query", {}).get(view.get("query_id")))
            and (row["task_id"], row["domain"]) not in refreshed
        ]
        work["last_candidate_views"] = kept + views
        work["last_operation"] = "query"
        return self.service.save_result(work, args, expected, operation_id)
