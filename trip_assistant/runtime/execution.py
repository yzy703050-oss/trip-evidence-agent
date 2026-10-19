"""Preflight the whole batch before starting any side effect."""

import asyncio
import json

from trip_assistant.presentation.facts import valid_save_receipt
from trip_assistant.presentation.trip_reply import validate_trip_reply
from trip_assistant.runtime.actions import finish_native, native_actions
from trip_assistant.runtime.errors import ToolFailure, error_message, error_payload
from trip_assistant.runtime.evidence_refresh import (
    refresh_observation,
    selection_refresh,
)
from trip_assistant.runtime.limits import LimitError
from trip_assistant.runtime.progress import report_progress
from trip_assistant.runtime.scheduler import gather_owned
from trip_assistant.runtime.telemetry import model_scope
from trip_assistant.runtime.trip_jobs import enqueue_trip_query, run_trip_jobs
from trip_assistant.storage.actions import ActionStore
from trip_assistant.tools.normalization import normalize_tool_arguments


async def execute_batch(calls, *, registry, turn, budget, sessions, resuming=False):
    turn_id = turn["turn_id"]
    turn["budget"] = budget
    turn["actions"] = ActionStore(sessions)
    if not resuming:
        sessions.record_calls(calls, turn_id, content=turn.get("assistant_text"))
    prepared = []
    query_keys = []
    failed_call_id = None
    refreshes = {}
    try:
        # Rejected TripPlan arguments are still planning interactions.
        if not resuming:
            budget.reserve_batch(
                calls,
                [],
                any(
                    c["name"] in registry and registry[c["name"]].category == "complex_write"
                    for c in calls
                ),
            )
        if len({c["id"] for c in calls}) != len(calls):
            raise ToolFailure("DUPLICATE_CALL_ID", "duplicate_call_id")
        for call in calls:
            failed_call_id = call["id"]
            if call["name"] not in registry:
                raise ToolFailure(
                    "UNKNOWN_TOOL", "unknown_tool:" + call["name"], {"tool": call["name"]}
                )
            tool = registry[call["name"]]
            args = tool.validate(normalize_tool_arguments(call["name"], call["arguments"], turn))
            if tool.followup:
                args["tripplan"] = tool.followup.service.normalize_input(args["tripplan"])
            if call["name"] == "write_trip" and args.get("reply") is not None and tool.followup:
                validate_trip_reply(
                    args["reply"],
                    args["tripplan"],
                    tool.followup.service.store.load(args["tripplan"]["trip_plan_id"])
                    if args["tripplan"].get("trip_plan_id")
                    else None,
                )
            prepared.append((call, tool, args))
        failed_call_id = None
        if any(call["name"] == "request_replan" for call, _, _ in prepared) and len(prepared) != 1:
            raise ToolFailure("REPLAN_BATCH_CONFLICT", "request_replan must be submitted alone")
        if turn.get("execution_machine"):
            turn["execution_machine"].guard(
                [
                    {"id": call["id"], "name": call["name"], "arguments": args}
                    for call, _, args in prepared
                ]
            )
        targets = [
            a["tripplan"]["trip_plan_id"]
            for _, tool, a in prepared
            if tool.category == "complex_write" and a.get("tripplan", {}).get("trip_plan_id")
        ]
        if len(targets) != len(set(targets)):
            raise ToolFailure("DUPLICATE_TRIP_WRITE", "one TripPlan write per batch")
        for call, tool, args in prepared:
            failed_call_id = call["id"]
            expand = tool.expand_queries or getattr(tool.handler, "expand_queries", None)
            try:
                if expand:
                    query_keys.extend(expand(args, turn))
                elif call["name"] == "query":
                    query_keys.extend(
                        json.dumps(
                            {"domain": q["domain"], "parameters": q["parameters"]},
                            sort_keys=True,
                            ensure_ascii=False,
                        )
                        for q in args["query_plan"]
                    )
                if tool.followup:
                    query_keys.extend(tool.followup.expand_queries(args, turn))
            except ToolFailure as error:
                if error.code != "EVIDENCE_EXPIRED" or not tool.followup:
                    raise
                plan = selection_refresh(tool.followup, args, error_payload(error), turn)
                if plan is None:
                    raise
                refreshes[call["id"]] = (plan, error_payload(error))
                query_keys.extend(
                    tool.followup.service.queries.expand_queries(
                        {"query_plan": [r["item"] for r in plan["requests"]]}, turn
                    )
                )
        failed_call_id = None
        if len({a["request_kind"] for _, _, a in prepared}) > 1:
            raise ToolFailure("INCONSISTENT_REQUEST_KIND", "inconsistent_request_kind")
        categories = {tool.category for _, tool, _ in prepared}
        if "policy" in categories and any(
            tool.category == "complex_write"
            and args["tripplan"]["operation"] not in {"pause", "cancel"}
            for _, tool, args in prepared
        ):
            raise ToolFailure("POLICY_OBSERVATION_REQUIRED", "policy_observation_required")
        budget.check_external_queries(query_keys)
    except (ValueError, LimitError) as error:
        failure = error_payload(error)
        source_id = failure["details"].get("call_id") or failed_call_id
        results = [
            {
                "call_id": c["id"],
                "name": c["name"],
                "status": "rejected",
                "reason": error_message(error),
                "error": failure
                if source_id in {None, c["id"]}
                else {"code": "BATCH_ABORTED", "details": {"cause": failure, "call_id": source_id}},
            }
            for c in calls
        ]
        for result in results:
            finish_native(sessions, turn_id, result)
        return results
    store = ActionStore(sessions)
    native_actions(
        sessions,
        turn_id,
        [
            {**call, "arguments": args}
            for call, _, args in prepared
            if store.get(turn_id + ":" + call["id"]) is None
        ],
    )
    turn.setdefault("query_attempts", {})

    async def execute(call, tool, args):
        report_progress(
            {
                "read": "正在读取相关资料…",
                "query": "正在查询实时出行信息…",
                "write_memory": "正在保存长期偏好…",
                "query_policy": "正在检索适用差旅政策…",
                "request_replan": "正在调整整体规划策略…",
                "write_trip": "正在保存行程查询地图…"
                if args.get("tripplan", {}).get("operation", "propose") == "propose"
                else "正在检查并保存行程…",
            }[call["name"]]
        )
        operation_id = turn_id + ":" + call["id"]
        if not store.claim(operation_id):
            existing = store.get(operation_id)
            if existing and existing["state"] in {"completed", "unknown", "rejected"}:
                return store.result(existing)
            raise ToolFailure("UNKNOWN_OPERATION", "operation already running")
        sessions.checkpoint_turn(
            turn_id,
            {
                **budget.checkpoint(),
                "query_attempts": turn["query_attempts"],
            },
        )
        try:
            budget.require_time()
            if call["id"] in refreshes:
                plan, failure = refreshes[call["id"]]
                result = refresh_observation(sessions, turn, call, plan, failure)
            else:
                with model_scope(tool_call_id=call["id"], tool_name=call["name"]):
                    async with asyncio.timeout(budget.remaining_seconds()):
                        value = await tool.handler(args, {**turn, "operation_id": operation_id})
                result = {"call_id": call["id"], "name": call["name"], **value}
            if (
                call["name"] == "write_trip"
                and result.get("status") == "completed"
                and not valid_save_receipt(result)
            ):
                raise ToolFailure("INVALID_SAVE_RECEIPT", "write result has no valid save receipt")
        except Exception as error:
            result = {
                "call_id": call["id"],
                "name": call["name"],
                "status": "error",
                "reason": error_message(error),
                "error": error_payload(error),
            }
            if (
                tool.followup
                and isinstance(error, ToolFailure)
                and error.code == "EVIDENCE_EXPIRED"
            ):
                try:
                    plan = selection_refresh(tool.followup, args, error_payload(error), turn)
                    if plan:
                        budget.check_external_queries(
                            tool.followup.service.queries.expand_queries(
                                {"query_plan": [r["item"] for r in plan["requests"]]}, turn
                            )
                        )
                        result = refresh_observation(
                            sessions, turn, call, plan, error_payload(error)
                        )
                except (ValueError, LimitError) as recovery_error:
                    result.update(reason=str(recovery_error), error=error_payload(recovery_error))
        finish_native(sessions, turn_id, result)
        if tool.followup and result.get("trip_plan_id"):
            enqueue_trip_query(sessions, turn, call["id"], result)
        sessions.checkpoint_turn(
            turn_id,
            {
                **budget.checkpoint(),
                "query_attempts": turn["query_attempts"],
            },
        )
        return result

    # Preference/policy observations precede writes; explicit queries consume saved maps.
    results = []
    pending = []
    memory_failed = False
    priority = {
        "simple_write": 0,
        "policy": 1,
        "read": 2,
        "query": 4,
        "complex_write": 3,
        "control": 0,
    }
    for call, tool, args in sorted(prepared, key=lambda row: priority[row[1].category]):
        if (
            memory_failed
            and args["request_kind"] == "planning"
            and tool.category in {"policy", "query", "complex_write"}
        ):
            result = {
                "call_id": call["id"],
                "name": call["name"],
                "status": "rejected",
                "reason": "preference_write_failed",
                "error": {"code": "PREFERENCE_WRITE_FAILED", "details": {}},
            }
            finish_native(sessions, turn_id, result)
            results.append(result)
            continue
        if tool.category in {"policy", "read", "query"}:
            pending.append(execute(call, tool, args))
        else:
            if pending:
                results.extend(await asyncio.gather(*pending))
                pending = []
            result = await execute(call, tool, args)
            results.append(result)
            if tool.category == "simple_write" and result.get("status") != "ok":
                memory_failed = True
    executor = next((tool.followup for _, tool, _ in prepared if tool.followup), None)

    async def automatic_queries():
        return await run_trip_jobs(executor, turn, sessions) if executor else []

    independent, jobs = await gather_owned(asyncio.gather(*pending), automatic_queries())
    results.extend(independent)
    by_id = {r["call_id"]: r for r in results}
    return [by_id[c["id"]] for c in calls] + jobs
