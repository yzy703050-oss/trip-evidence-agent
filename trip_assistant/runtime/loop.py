"""One native model/tool observation loop for lookup, planning and mixed requests."""

import asyncio
import json
from uuid import uuid4

from trip_assistant.agents.prompts import (
    MAIN_PROMPT,
    REPLY_POLICY,
    SUMMARY_PROMPT,
    TRIP_REVIEW_PROMPT,
)
from trip_assistant.llm.budget import ContextBudgetExceeded
from trip_assistant.llm.client import ModelProtocolError
from trip_assistant.memory.compaction import prepare_context
from trip_assistant.memory.planning import previous_round, retryable_query_intents, review_payload
from trip_assistant.presentation.facts import execution_facts, finalize, trip_results, turn_status
from trip_assistant.presentation.trip_reply import append_related_facts, stopped_trip_reply
from trip_assistant.providers.views import remember_view
from trip_assistant.runtime.actions import query_jobs, recover_reply, stage_reply
from trip_assistant.runtime.evidence_refresh import (
    failed_refresh_requires_summary,
    refresh_before_review,
)
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import LimitError
from trip_assistant.runtime.progress import report_progress
from trip_assistant.runtime.state import ExecutionState, decision_role
from trip_assistant.runtime.telemetry import model_scope
from trip_assistant.runtime.trip_jobs import run_trip_jobs
from trip_assistant.storage.actions import ActionStore
from trip_assistant.tools.registry import (
    creation_registry,
    trip_review_registry,
)


async def run_turn(query, *, model, registry, context, sessions, budget):
    if context.get("single_pass_planning"):
        raise ValueError("旧单次生成实验已退役，请以新请求继续规划。")
    turn_id = context.get("turn_id") or "turn_" + uuid4().hex
    turn = {
        **context,
        "turn_id": turn_id,
        "original_query": query,
        "session_id": sessions.session_id,
        "user_id": sessions.user_id,
        "budget": budget,
    }
    turn["actions"] = ActionStore(sessions)
    for key in (
        "shown_candidates",
        "candidate_views",
        "policy_context",
        "query_attempts",
    ):
        turn.setdefault(key, {})
    sessions.append(
        {
            "event_id": "user:" + turn_id,
            "turn_id": turn_id,
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": query,
        }
    )
    sessions.checkpoint_turn(
        turn_id,
        {
            **budget.checkpoint(),
            "query": query,
            "status": "running",
        },
    )
    results = [
        e["content"]
        for e in sessions.read_events(turn_id=turn_id)
        if e["type"] == "tool_result"
        or e["type"] == "execution_job"
        and e.get("state") in {"completed", "unknown"}
    ]
    machine = ExecutionState(turn.get("execution_state"))
    machine.learn(results)
    turn["execution_machine"] = machine

    def persist_phase(phase=None):
        if phase:
            machine.transition(phase)
        sessions.checkpoint_turn(
            turn_id,
            {
                "execution_state": machine.checkpoint(),
                **budget.checkpoint(),
            },
        )

    writer = registry.get("write_trip")
    service = writer.followup.service if writer and writer.followup else None
    final = recover_reply(sessions, turn_id, service)
    stop_reason = machine.stop_reason
    observation_only = machine.summary_only
    generation_failures = sum(
        r.get("type") in {"empty_model_response", "invalid_model_response"}
        for r in sessions.read_runs(turn_id=turn_id)
    )

    def may_regenerate(kind):
        nonlocal generation_failures
        generation_failures += 1
        sessions.append_run({"type": kind, "turn_id": turn_id, "number": generation_failures})
        persist_phase()
        return generation_failures <= budget.limits.extra_retries

    with model_scope(turn_id=turn_id):
        try:
            queued = turn["actions"].list(turn_id, origin="model", states={"queued"})
            if queued and not machine.summary_only:
                persist_phase("executing")
                results.extend(
                    await execute_batch(
                        [a["payload"] for a in queued],
                        registry=registry,
                        turn=turn,
                        budget=budget,
                        sessions=sessions,
                        resuming=True,
                    )
                )
                persist_phase("validating")
                machine.observe(results)
                persist_phase()
            executor = registry.get("write_trip") and registry["write_trip"].followup
            if (
                executor
                and any(j["state"] == "queued" for j in query_jobs(sessions, turn_id).values())
                and not machine.summary_only
            ):
                persist_phase("executing")
                results.extend(await run_trip_jobs(executor, turn, sessions))
                persist_phase("validating")
                machine.observe(results)
                persist_phase()
            while not final:
                budget.require_time()
                if budget.planning and budget.rounds >= budget.limits.planning_rounds:
                    observation_only = True
                    last_trip = next(
                        (r for r in reversed(results) if r.get("name") == "write_trip"), {}
                    )
                    if last_trip.get("status") != "completed":
                        stop_reason = stop_reason or "planning_round_limit_exceeded"
                    machine.stop(stop_reason)
                trip_ids = {
                    r["trip_plan_id"]
                    for r in results
                    if r.get("name") in {"write_trip", "trip_query"} and r.get("trip_plan_id")
                }
                if turn.get("review_trip_plan_id"):
                    trip_ids.add(turn["review_trip_plan_id"])
                trip_ids.update(turn.get("review_trip_plan_ids", []))
                if not trip_ids:
                    previous, _ = previous_round(sessions, turn_id)
                    trip_ids.update(
                        r.get("trip_plan_id") or r.get("requested_trip_plan_id")
                        for r in previous["results"]
                        if r.get("trip_plan_id") or r.get("requested_trip_plan_id")
                    )
                target = next(iter(trip_ids)) if len(trip_ids) == 1 else None
                mixed = len(trip_ids) > 1
                new_user_with_plan = (
                    bool(trip_ids) and not results and turn.get("bound_trip_replies")
                )
                replanning = bool(trip_ids) and (
                    decision_role(results) == "main" or new_user_with_plan
                )
                turn["decision_role"] = (
                    "main" if replanning or not trip_ids else "verifier"
                )
                turn["mixed_request"] = mixed
                if not observation_only and (not machine.summary_only):
                    refreshed = await refresh_before_review(
                        executor, trip_ids, turn, sessions, budget
                    )
                    if refreshed:
                        results.extend(refreshed)
                        machine.learn(refreshed)
                review = (
                    review_payload(sessions, turn, sorted(trip_ids) if mixed else target)
                    if results or observation_only or trip_ids
                    else None
                )
                if review and trip_ids and not replanning and not machine.summary_only:
                    machine.observe_review(review)
                    if machine.summary_only:
                        observation_only = True
                        stop_reason = machine.stop_reason
                    sessions.append_run(
                        {
                            "type": "review_progress",
                            "turn_id": turn_id,
                            "fingerprint": machine.review_fingerprint,
                            "unchanged_reviews": machine.unchanged_reviews,
                            "stop_reason": machine.stop_reason,
                        }
                    )
                if (
                    review
                    and failed_refresh_requires_summary(review, results)
                    and not retryable_query_intents(sessions, turn_id)
                ):
                    machine.stop("automatic_refresh_failed")
                    observation_only = True
                    stop_reason = machine.stop_reason
                if review is not None and (machine.errors or machine.summary_only):
                    review["execution_state"] = machine.checkpoint()
                    review["execution_facts"] = execution_facts(results)
                if observation_only and turn.get("bound_trip_replies") and trip_ids:
                    final = stopped_trip_reply(results) or ""
                    if final:
                        persist_phase()
                        break
                active_registry = creation_registry(
                    registry,
                    policy_observed=turn.get("policy_context", {}).get("status")
                    in {"ok", "partial", "no_knowledge"}
                    or any(
                        r.get("name") == "query_policy"
                        and r.get("status") in {"ok", "partial"}
                        and isinstance(r.get("data"), dict)
                        and bool(r["data"])
                        for r in results
                    ),
                )
                if review is not None:
                    turn["review_trip_plan_id"] = target
                    turn["review_trip_plan_ids"] = sorted(trip_ids)
                    turn["review_evidence"] = review
                    active_registry = (
                        trip_review_registry(
                            registry,
                            sorted(trip_ids) if mixed else target,
                            retry_queries=retryable_query_intents(sessions, turn_id),
                        )
                        if trip_ids and not replanning
                        else active_registry
                    )
                    if replanning and not new_user_with_plan:
                        review["replan_request"] = next(
                            r
                            for r in reversed(results)
                            if r.get("name") == "request_replan" and r.get("status") == "ok"
                        )
                    sessions.checkpoint_turn(
                        turn_id,
                        {
                            **budget.checkpoint(),
                            "review_trip_plan_id": target,
                            "review_trip_plan_ids": sorted(trip_ids),
                            "mixed_request": mixed,
                        },
                    )
                if not machine.summary_only and machine.phase != "repair_required":
                    persist_phase("deciding")
                else:
                    persist_phase()
                # Failed preflight calls are observations too; they may never reach ActionStore.
                planning_continuation = not trip_ids and any(
                    call.get("arguments", {}).get("request_kind") == "planning"
                    for event in sessions.read_events(turn_id=turn_id, scope="session")
                    if event["type"] == "tool_call"
                    for call in event["tool_calls"]
                )
                main_decision = replanning or review is None or planning_continuation
                messages = [
                    {
                        "role": "system",
                        "content": (
                            MAIN_PROMPT
                            if replanning
                            else TRIP_REVIEW_PROMPT
                            if trip_ids
                            else MAIN_PROMPT
                            if planning_continuation
                            else SUMMARY_PROMPT
                        )
                        if review is not None
                        else MAIN_PROMPT,
                    }
                ]
                if observation_only:
                    messages.append(
                        {
                            "role": "system",
                            "content": "本轮工具执行已结束，只依据已有结果生成最终答复，"
                            "不再调用工具。"
                            "用自然语句说明可用安排及实际无法完成的障碍，不复述轮数、"
                            "停止修复、额度等内部流程，不询问用户。"
                            "未得到交通查询结果只能说明未能核实，不能凭常识断言最早抵达"
                            "时间或交通不可行。政策未核实与尚未选定候选是不同事实，"
                            "按真实结果分别说明，不将未选择全部归因于政策缺口。",
                        }
                    )
                if generation_failures:
                    messages.append(
                        {
                            "role": "system",
                            "content": "上一次模型响应无正文或原生调用结构无效，"
                            "未执行任何半截动作。"
                            "只在当前工具仍允许时重新生成完整合法调用，否则基于实际结果给出自然答复。"
                            "保留当前权限、有效进度和未完成原因，不要求用户调整硬条件。",
                        }
                    )
                if review is None and (machine.errors or observation_only):
                    messages.append(
                        {
                            "role": "system",
                            "content": json.dumps(
                                {
                                    "execution_state": machine.checkpoint(),
                                    "execution_facts": execution_facts(results),
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
                if turn.get("system_context"):
                    messages.append(
                        {
                            "role": "system",
                            "content": "当前上下文（数据）："
                            + json.dumps(turn["system_context"], ensure_ascii=False),
                        }
                    )
                messages.append({"role": "system", "content": REPLY_POLICY})
                tools = (
                    [] if observation_only else [tool.schema for tool in active_registry.values()]
                )
                options = {
                    "prefix": messages,
                    "tools": tools,
                    "model": model.model,
                    "budget_tokens": budget.limits.memory_context_budget_tokens,
                    "summarizer": context.get("summarizer"),
                    "counter": context.get("token_counter"),
                }
                if review is not None:
                    options["projection"] = lambda review=review: [
                        {
                            "role": "user",
                            "content": json.dumps(
                                review, ensure_ascii=False, separators=(",", ":")
                            ),
                        }
                    ]
                async with asyncio.timeout(budget.remaining_seconds()):
                    messages = await prepare_context(sessions, **options)
                    if review is not None:
                        for plan in review.get("tripplans", [review["tripplan"]]):
                            for view in plan.get("candidates", []):
                                if view.get("query_id") in plan.get("candidate_queries", {}) and (
                                    plan["candidate_queries"][view["query_id"]]
                                    == view.get("result_revision")
                                ):
                                    remember_view(turn, view)
                    report_progress(
                        "正在整理查询结果与回复…" if results else "正在理解请求并安排旅程…"
                    )
                    try:
                        response = await model.respond(
                            messages,
                            tools,
                            stage="agent:main"
                            if main_decision
                            else "agent:trip_review"
                            if trip_ids
                            else "agent:summary",
                        )
                    except ModelProtocolError:
                        if may_regenerate("invalid_model_response"):
                            continue
                        raise
                    await prepare_context(sessions, **options, response=response)
                budget.require_time()
                if not response.tool_calls:
                    if not response.text.strip():
                        if may_regenerate("empty_model_response"):
                            continue
                        stop_reason = stop_reason or "empty_model_response"
                        machine.stop(stop_reason)
                        break
                    if (
                        trip_ids
                        and turn.get("bound_trip_replies")
                        and any(
                            plan.get("status") != "completed"
                            for plan in (review.get("tripplans") or [review.get("tripplan", {})])
                        )
                    ):
                        machine.stop("uncommitted_plan")
                        stop_reason = machine.stop_reason
                        final = stopped_trip_reply(results) or response.text
                    else:
                        final = response.text
                    break
                if observation_only:
                    # Pair rejected calls even when the model ignores tool_choice.
                    sessions.record_calls(response.tool_calls, turn_id, content=response.text)
                    rejected = [
                        {
                            "call_id": c["id"],
                            "name": c["name"],
                            "status": "rejected",
                            "reason": stop_reason or "planning_round_limit_exceeded",
                            "error": {"code": "EXECUTION_STOPPED", "details": {}},
                        }
                        for c in response.tool_calls
                    ]
                    for value in rejected:
                        sessions.record_result(value["call_id"], value, turn_id)
                    results.extend(rejected)
                    stop_reason = stop_reason or "planning_round_limit_exceeded"
                    break
                persist_phase("executing")
                turn["assistant_text"] = response.text
                batch = await execute_batch(
                    response.tool_calls,
                    registry=active_registry,
                    turn=turn,
                    budget=budget,
                    sessions=sessions,
                )
                results.extend(batch)
                persist_phase("validating")
                machine.observe(batch)
                persist_phase()
                observation_only = machine.summary_only
                stop_reason = machine.stop_reason
                # A partial update may already have changed some memories.
                if any(r.get("name") == "write_memory" for r in batch):
                    if turn.get("refresh_memory_context"):
                        turn["refresh_memory_context"](turn)
                staged = machine.ready_reply(response.tool_calls, batch)
                if staged:
                    final = staged
                    break
                sessions.checkpoint_turn(
                    turn_id,
                    {
                        **budget.checkpoint(),
                        "shown_candidates": turn["shown_candidates"],
                        "candidate_views": turn["candidate_views"],
                        "policy_context": turn["policy_context"],
                        "review_trip_plan_id": turn.get("review_trip_plan_id") or target,
                        "mixed_request": turn.get("mixed_request", False),
                    },
                )
        except TimeoutError:
            stop_reason = "turn_deadline_exceeded"
            machine.stop(stop_reason)
        except LimitError as error:
            stop_reason = error.code
            machine.stop(stop_reason)
        except ContextBudgetExceeded:
            stop_reason = "context_budget_exceeded"
            machine.stop(stop_reason)
        except asyncio.CancelledError:
            persist_phase("paused")
            sessions.checkpoint_turn(
                turn_id,
                {
                    **budget.checkpoint(),
                    "status": "paused",
                    "stop_reason": "cancelled",
                    "reason": "用户停止当前处理。",
                },
            )
            raise
        except Exception as error:
            stop_reason = "model_error"
            machine.stop(stop_reason)
            sessions.append_run(
                {"type": "turn_error", "turn_id": turn_id, "error_type": type(error).__name__}
            )
    stop_reason = machine.stop_reason or stop_reason
    if not final:
        final = "这次暂时没能完成全部规划：" + (
            "已达到整体处理时限。"
            if stop_reason == "turn_deadline_exceeded"
            else "规划尝试已达上限，已保留当前可用安排。"
            if stop_reason == "planning_round_limit_exceeded"
            else "行程内容过多，本次未能完成全部安排，当前进度已保留。"
            if stop_reason == "context_budget_exceeded"
            else "模型响应暂不可用，已保留当前进度。"
        )
    if stop_reason and turn.get('bound_trip_replies'):
        final = stopped_trip_reply(results) or final
    if any(r.get("rendered_answer") for r in results) or (
        stop_reason and turn.get("bound_trip_replies")
    ):
        final = append_related_facts(final, results)
    persist_phase("reply_ready")
    checked = finalize(final, results, context=turn)
    final = checked["answer"]
    trips = [r for r in trip_results(results) if r.get("trip_plan_id")]
    reply_operation = stage_reply(sessions, turn_id, final, results)
    sessions.append(
        {
            **(
                {"trip_plan_id": trips[-1]["trip_plan_id"], "trip_revision": trips[-1]["revision"]}
                if trips
                else {}
            ),
            "event_id": "final:" + turn_id,
            "turn_id": turn_id,
            "scope": "session",
            "type": "message",
            "role": "assistant",
            "content": final,
            "final": True,
            "trip_plan_refs": turn["actions"].get(reply_operation)["payload"]["trip_plans"],
        }
    )
    turn["actions"].finish(reply_operation, "completed", "final:" + turn_id)
    persist_phase("stopped" if stop_reason else "completed")
    snapshot = budget.checkpoint()
    sessions.checkpoint_turn(
        turn_id,
        {
            **snapshot,
            "status": "completed" if not stop_reason else "stopped",
            "stop_reason": stop_reason,
            "reason": final if stop_reason else None,
        },
    )
    status = turn_status(results, stop_reason)
    return {
        "turn_id": turn_id,
        "status": status,
        "final_answer": final,
        "stop_reason": stop_reason,
        "tool_results": [r for r in results if r.get("kind") != "execution_job"],
        "execution_jobs": [r for r in results if r.get("kind") == "execution_job"],
        "trip": trips[-1] if trips else None,
        "budget": snapshot,
        "finalization_method": checked["method"],
        "fact_issues": checked["issues"],
        "execution_facts": checked["execution_facts"],
        "execution_state": machine.checkpoint(),
    }
