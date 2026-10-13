"""Bounded main-agent decisions over durable, task-scoped travel state."""

from context.context_budget import ContextBudgetExceeded
from context.resource_budget import (
    ModelCallLimitExceeded,
    is_calibrating,
    successful_information_result,
)
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
import math
from sqlite3 import Error as SQLiteError

from agents.workflow_contracts import task_by_id, validate_action, identifier, effective_conditions
from agents.workflow_queries import query_views
from agents.workflow_guard import check_task, check_workflow, finalize_workflow
from agents.planning_conditions import prepare_task
from agents.workflow_repair import apply_repair, advance_repair
from context.telemetry import model_scope
from context.workflow_store import WorkflowConflictError
from travel_data.candidates import CandidateStore
from agents.context_projection import (
    project_child_result,
    project_domain_result,
    project_workflow_context,
    project_delegation_context,
    decision_requirements,
)


@dataclass(frozen=True)
class WorkflowLimits:
    info_executions_per_task: int = 3
    main_steps_per_task: int = 4
    main_extra_steps: int = 4
    tools_per_task: int = 10
    turn_timeout: float = 600.0
    evidence_max_age_seconds: float = 3600.0
    main_decisions_per_turn: int | None = None
    main_model_calls_per_turn: int | None = None
    main_model_calls_per_task: int | None = None

    def __post_init__(self):
        for name in (
            "main_decisions_per_turn",
            "main_model_calls_per_turn",
            "main_model_calls_per_task",
        ):
            val = getattr(self, name)
            if val is not None and (type(val) is not int or val < 1):
                raise ValueError("invalid successful resource limit")
        for name in (
            "info_executions_per_task",
            "main_steps_per_task",
            "main_extra_steps",
            "tools_per_task",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("invalid workflow limit")
        if not math.isfinite(self.turn_timeout) or self.turn_timeout <= 0:
            raise ValueError("invalid workflow timeout")
        if not math.isfinite(self.evidence_max_age_seconds) or self.evidence_max_age_seconds <= 0:
            raise ValueError("invalid evidence age")


class WorkflowRunner:
    def __init__(
        self,
        main_agent,
        info_agent=None,
        memory_manager=None,
        *,
        limits=None,
        emit=None,
        agent_registry=None,
        tool_executor=None,
    ):
        from config import WORKFLOW_LIMITS

        self.main, self.info, self.memory = main_agent, info_agent, memory_manager
        self.limits, self.emit = limits or WorkflowLimits(**WORKFLOW_LIMITS), emit
        self.agent_registry = agent_registry if agent_registry is not None else {}
        from travel_data.tools import ToolExecutor

        providers = getattr(self.agent_registry, "providers", {})
        # Legacy injected agents may carry providers, but are never run or loaded.
        self.executor = (
            tool_executor
            or (getattr(info_agent, "tool_executor", None) if info_agent else None)
            or ToolExecutor(providers)
        )
        self._request_active = False

    @staticmethod
    def _request_result(status, answer, **extra):
        return {
            "status": status,
            "finalization_method": "direct",
            "final_answer": answer,
            "results": [],
            "domain_results": {},
            "missing_fields": [],
            **extra,
        }

    async def run_request(self, context, run):
        from context.resource_budget import success_budget_scope

        with (
            success_budget_scope(
                main_per_turn=self.limits.main_model_calls_per_turn,
                main_per_task=self.limits.main_model_calls_per_task,
            ),
            model_scope(turn_id=run.turn_id),
        ):
            self._request_active = True
            try:
                result = await asyncio.wait_for(
                    self._request_flow(deepcopy(context), run), self.limits.turn_timeout
                )
                from utils.response_renderer import finalize_business_result

                return finalize_business_result(result)
            except ContextBudgetExceeded as exc:
                return self.budget_stop(run, exc)
            except ModelCallLimitExceeded:
                return self._request_result(
                    "partial",
                    "本轮模型调用达到上限，已有资料保留。",
                    stop_reason="main_model_call_limit",
                )
            except asyncio.TimeoutError:
                if run.workflow:
                    return self._stop("partial", "turn_timeout", "本轮时间上限已到，已有结果保留。")
                return self._request_result(
                    "partial", "本轮时间上限已到。", stop_reason="turn_timeout"
                )
            except WorkflowConflictError:
                return self._request_result(
                    "error",
                    "方案版本已经变化，请按最新版本继续。",
                    stop_reason="workflow_revision_conflict",
                )
            except (OSError, SQLiteError):
                return self._request_result(
                    "error",
                    "状态保存失败，已停止，不自动重放查询。",
                    stop_reason="workflow_save_failed",
                    error_code="workflow_save_failed",
                )
            except (ValueError, KeyError, TypeError, ArithmeticError):
                return self._request_result(
                    "error", "请求或模型动作无效，未继续执行。", error_code="invalid_plan"
                )
            except Exception:
                return self._request_result(
                    "error",
                    "模型服务未完成本轮决定，已有资料保留。",
                    stop_reason="model_request_failed",
                )
            finally:
                self._request_active = False

    def budget_stop(self, run, exc):
        from context.context_budget import CONTEXT_BUDGET_MESSAGE

        result = self._request_result(
            "partial",
            CONTEXT_BUDGET_MESSAGE,
            stop_reason="context_budget_exceeded",
            budget={
                "agent_scope": exc.agent_scope,
                "phase": exc.phase,
                "tokens": exc.estimate.tokens,
                "method": exc.estimate.method,
                "estimated": exc.estimate.estimated,
                "budget_tokens": exc.budget_tokens,
            },
        )
        if run.workflow:
            result.update(
                workflow=deepcopy(run.workflow),
                workflow_id=run.workflow["id"],
                workflow_revision=run.workflow["revision"],
            )
        return result

    async def _query_policy_context(self, context, run):
        from agents.policy_preflight import TravelPolicyAgent

        try:
            retriever = self.agent_registry["rag_knowledge"]
            policy_agent = TravelPolicyAgent(getattr(self.main, "model", None), retriever)
            policy = await policy_agent.query(context)
            if self.memory and policy_agent.evidence:
                self.memory._record_event(
                    {
                        "turn_id": run.turn_id,
                        "scope": "run",
                        "type": "policy_evidence",
                        "content": policy_agent.evidence,
                    }
                )
        except (ContextBudgetExceeded, ModelCallLimitExceeded):
            raise
        except Exception as exc:
            if self.memory:
                self.memory._record_event(
                    {
                        "turn_id": run.turn_id,
                        "scope": "run",
                        "type": "policy_validation_error",
                        "content": {"error_type": type(exc).__name__, "reason": str(exc)[:240]},
                    }
                )
            policy = {
                "status": "error",
                "constraints": [],
                "applicability": {},
                "references": [],
                "effective_date": None,
                "missing_fields": ["policy_evidence"],
                "conflicts": [],
            }
        return policy

    async def _request_flow(self, context, run):
        from agents.memory_tools import read_memory
        from agents.contracts import validate_plan
        from agents.workflow_contracts import create_workflow, apply_user_update
        from agents.workflow_state import STOPPED

        if context.get("read_request"):
            return self._read_request(context["read_request"], context)
        memory_result = await self.run_memory_request(context, run)
        if memory_result is not None:
            return memory_result
        if self.memory:
            actual = read_memory(self.memory, {})
            run.effective_preferences = {
                **actual["legacy_preferences"],
                "memories": actual["memories"],
            }
        context["effective_preferences"] = deepcopy(run.effective_preferences)
        context.setdefault("default_round_trip", True)
        from agents.planning_conditions import ambiguous_date_options

        context["ambiguous_date_options"] = ambiguous_date_options(
            context["original_query"], context.get("current_time")
        )
        if self.memory:
            context["known_workflows"] = self.memory.get_known_workflows(
                query=context["original_query"]
            )
            state = self.memory.session_store.read_state()
            context["active_workflows"] = [
                w
                for w in context["known_workflows"]
                if w["id"] in state.get("active_workflow_ids", [])
                and w.get("stop_reason") not in STOPPED
            ]
            if state.get("pending_workflow_match"):
                context["pending_workflow_match"] = state["pending_workflow_match"]
            from agents.preference_analysis import (
                get_analysis_trigger,
                PreferenceAnalysisAgent,
                save_preference_analysis,
            )

            explicit = (
                "重新" in context["original_query"]
                and "偏好" in context["original_query"]
                and ("总结" in context["original_query"] or "分析" in context["original_query"])
            )
            history = [
                {**w, "trip_plan_id": w["id"]} for w in self.memory.workflow_store.list_summaries()
            ]
            trigger = get_analysis_trigger(
                history, actual["analysis"], explicit_reanalysis_requested=explicit
            )
            planning = any(
                word in context["original_query"]
                for word in ("规划", "安排", "旅游", "出差", "行程", "去")
            )
            if trigger and (explicit or planning) and getattr(self.main, "model", None):
                try:
                    analyzed = await PreferenceAnalysisAgent(self.main.model).analyze(
                        {"history": history, "memories": actual["memories"]}
                    )
                    save_preference_analysis(self.memory, analyzed, trigger)
                    refreshed = read_memory(self.memory, {})
                    run.effective_preferences["memories"] = refreshed["memories"]
                    context["effective_preferences"] = deepcopy(run.effective_preferences)
                except (ContextBudgetExceeded, ModelCallLimitExceeded):
                    raise
                except (ValueError, OSError):
                    context["preference_analysis_gap"] = "历史偏好未能总结，继续使用已保存偏好。"
                if explicit:
                    return self._request_result(
                        "ok" if "preference_analysis_gap" not in context else "partial",
                        "历史偏好已总结保存。"
                        if "preference_analysis_gap" not in context
                        else context["preference_analysis_gap"],
                    )
        policy = context.get("policy_context")
        business = (
            any(
                word in context["original_query"]
                for word in ("出差", "商务", "客户验收", "客户拜访", "差旅政策", "报销制度")
            )
            or context.get("purpose") == "business"
        )
        if (
            business
            and policy is None
            and self.memory
            and any(word in context["original_query"] for word in ("换", "修改", "继续", "调整"))
        ):
            candidates = [
                w for w in context.get("known_workflows", []) if w.get("policy_available")
            ]
            if len(candidates) == 1 and not any(
                word in context["original_query"]
                for word in ("新政策", "最新政策", "新旅行", "另一趟")
            ):
                saved = self.memory.workflow_store.load(candidates[0]["id"])
                if saved and saved["revision"] == candidates[0]["revision"]:
                    policy = deepcopy(saved.get("policy_context"))
        if business and policy is None:
            policy = await self._query_policy_context(context, run)
        if policy is not None:
            context["policy_context"] = policy
        initial = getattr(self.main, "initialize", None) or getattr(self.main, "plan")
        with model_scope(turn_id=run.turn_id):
            decision = validate_plan(await initial(context))
        if self.memory:
            self.memory.session_store.append_run(
                {"type": "agent_plan", "turn_id": run.turn_id, "decision": decision, "agents": []}
            )
        if decision.get("workflow_match"):
            from agents.workflow_matching import validate_match

            match = validate_match(decision["workflow_match"], context.get("known_workflows", []))
            if match["action"] == "clarify":
                if self.memory:
                    self.memory.session_store.update_state(
                        {
                            "pending_workflow_match": {
                                "question": match["question"],
                                "candidates": deepcopy(context.get("known_workflows", [])),
                            }
                        }
                    )
                return self._request_result(
                    "needs_input", match["question"], stop_reason="workflow_match_required"
                )
            if match["action"] == "reuse":
                decision["response_mode"] = "workflow"
                decision["resume_workflow_id"] = match["workflow_id"]
                decision["refresh"] = match.get("refresh", False)
        # No legacy preference/memory/information Agent can be dispatched here.
        if decision.get("read_request"):
            return self._read_request(decision["read_request"], context)
        if decision["response_mode"] == "direct":
            scope = decision.get("feedback_scope")
            if scope and self.memory:
                w = self._load_target(scope["workflow_id"], context)
                w.update(
                    status="running",
                    stop_reason="feedback_scope_required",
                    checkpoint={
                        "kind": "feedback_scope",
                        "question": scope["question"],
                        "expected_workflow_revision": w["revision"] + 1,
                    },
                )
                w = self.memory.workflow_store.save(w, expected_revision=w["revision"])
                return self._request_result(
                    "needs_input", scope["question"], workflow=w, workflow_id=w["id"]
                )
            return self._request_result("ok", decision["final_answer"])
        if decision["response_mode"] != "workflow":
            from agents.workflow_batches import bind_query_plan
            from travel_data.batch_execution import execute_query_batch
            from travel_data.result_guard import grounded_answer

            plan = decision.get("query_plan", [])
            if not plan:
                return self._request_result(
                    "needs_input",
                    "请提供具体查询的城市、日期或查询对象。",
                    missing_fields=["query_plan"],
                )
            run.workflow_tool_budget = run.limits.info_tool_calls

            async def persist_queries():
                self._persist_query_progress(run)

            result = await execute_query_batch(
                bind_query_plan(plan, None, context), run, self.executor, after_tool=persist_queries
            )
            from travel_data.result_guard import guard_information_result

            guarded = guard_information_result(
                {"domain_results": run.domain_results}, [q["domain"] for q in plan]
            )
            run.domain_results = guarded["domain_results"]
            answer = "\n".join(
                grounded_answer({"domain_results": {r["domain"]: r}}) for r in result["results"]
            )
            return self._request_result(
                guarded["status"] if not result["gaps"] else "partial",
                answer or grounded_answer(guarded),
                finalization_method="tools",
                domain_results=deepcopy(run.domain_results),
                task_results=result["results"],
                gaps=result["gaps"],
            )
        identity = decision.get("resume_workflow_id")
        if identity:
            w = self._load_target(identity, context)
            if (
                w.get("pending_amendment")
                and not decision.get("travel_update")
                and not decision.get("amendment_patch")
            ):
                committed = deepcopy(w)
                staged = deepcopy(w["pending_amendment"])
                committed.pop("pending_amendment", None)
                staged.update(revision=w["revision"], previous_committed_plan=committed)
                w = staged
            if self.memory:
                self.memory.session_store.update_state({}, remove_keys=("pending_workflow_match",))
                self.memory.set_active_workflow(identity)
            if w.get("policy_context") and not policy:
                context["policy_context"] = deepcopy(w["policy_context"])
            if decision.get("amendment_patch"):
                return await self._amend_request(w, decision["amendment_patch"], context, run)
            update = decision.get("travel_update")
            if update:
                from agents.workflow_contracts import validate_quote_requirement

                validate_quote_requirement(update.get("condition_updates", {}), context)
                target = update.get("target", {})
                ids = target.get("task_ids", [])
                components = target.get("components", [])
                if (
                    update["update_type"] in {"replace", "change", "supplement"}
                    and len(ids) == 1
                    and components
                    and set(components) <= {"train", "hotel", "schedule"}
                    and not set(update.get("condition_updates", {}))
                    & {"origin", "destination", "requires_hotel", "purpose"}
                    and hasattr(self.main, "amend_saved_trip")
                ):
                    return await self._decide_amendment(w, update, context, run)
                from agents.travel_updates import apply_travel_update

                w = apply_travel_update(w, update, context=context)
            elif decision.get("workflow_update"):
                w = apply_user_update(w, decision["workflow_update"])
            elif w.get("stop_reason") in STOPPED:
                w.update(status="running", stop_reason=None)
            elif decision.get("refresh"):
                from agents.workflow_state import invalidate_query_binding
                from agents.workflow_batches import initialize_query_containers

                initialize_query_containers(w)
                scope = decision.get("refresh_target") or {
                    "task_ids": [t["id"] for t in w["tasks"]],
                    "components": ["train", "hotel"],
                }
                ids, components = scope.get("task_ids"), scope.get("components")
                if (
                    not isinstance(ids, list)
                    or not ids
                    or not isinstance(components, list)
                    or not components
                    or not set(components) <= {"train", "hotel"}
                ):
                    raise ValueError("invalid refresh scope")
                for identity in ids:
                    task_by_id(w, identity)
                w["refresh_scope"] = deepcopy(scope)
                for task in w["tasks"]:
                    if task["id"] not in ids:
                        continue
                    task.update(status="pending", draft_stage="query")
                    task["update_scope"] = sorted(set(components) & set(task["query_containers"]))
                    for domain, container in task["query_containers"].items():
                        if domain not in components:
                            continue
                        task["query_containers"][domain] = invalidate_query_binding(
                            container, "explicit_refresh"
                        )
                for row in w["results_by_query"].values():
                    if row["task_id"] in ids and row["domain"] in components:
                        row["needs_revalidation"] = True
                w.update(status="running", stop_reason=None, validation=None)
            elif w["status"] == "completed" and check_workflow(w)["valid"]:
                run.workflow = w
                self.w, self.run_state, self.context = w, run, context
                return self._finish_batch("workflow_reused", None)
        else:
            try:
                w = create_workflow(
                    decision["workflow_proposal"], context, require_stay_proposals=True
                )
            except ValueError as exc:
                if not hasattr(self.main, "repair_proposal"):
                    raise
                repaired = await self.main.repair_proposal(
                    context, decision["workflow_proposal"], str(exc)
                )
                w = create_workflow(
                    repaired.get("workflow_proposal", repaired),
                    context,
                    require_stay_proposals=True,
                )
        if not policy and any(t.get("purpose") == "business" for t in w["tasks"]):
            policy = await self._query_policy_context({**context, "purpose": "business"}, run)
        if policy:
            from agents.policy_preflight import resolve_policy_for_workflow

            policy = resolve_policy_for_workflow(policy, w, context)
            w["policy_context"] = deepcopy(policy)
            for task in w["tasks"]:
                for constraint in policy["constraints"]:
                    scope = constraint.get("applicability", {})
                    if scope.get("city") and scope["city"] != task["destination"]:
                        continue
                    field = constraint.get("field")
                    if field in {
                        "hotel_max_nightly_cny",
                        "hotel_max_total_cny",
                        "train_max_total_cny",
                        "seat_class",
                    }:
                        existing = task["conditions"].setdefault("constraints", {}).get(field)
                        proposed = constraint["value"]
                        if field == "seat_class" and existing is not None and existing != proposed:
                            policy["conflicts"].append(
                                {
                                    "code": "user_policy_conflict",
                                    "field": field,
                                    "task_id": task["id"],
                                }
                            )
                        from decimal import Decimal

                        task["conditions"]["constraints"][field] = (
                            str(min(Decimal(str(existing)), Decimal(str(proposed))))
                            if existing is not None and field.endswith("_cny")
                            else proposed
                        )
            w["policy_context"] = deepcopy(policy)
        if self.memory:
            w = self.memory.workflow_store.save(
                w, expected_revision=w["revision"] if identity else None
            )
            self.memory.set_active_workflow(w["id"])
        if w.get("stop_reason") in STOPPED:
            return self._request_result(
                "ok",
                "已停止规划并保存方案。",
                workflow=w,
                workflow_id=w["id"],
                workflow_revision=w["revision"],
                stop_reason=w["stop_reason"],
            )
        self.initial_query_plan = decision.get("query_plan")
        return await self.run(context, run, w)

    async def _decide_amendment(self, workflow, update, context, run):
        from agents.workflow_amendments import prepare_saved_trip_amendment
        from agents.workflow_contracts import validate_conditions

        task_id = update["target"]["task_ids"][0]
        authorized = deepcopy(update.get("condition_updates", {}))
        if "hotel_brands" in authorized:
            authorized["hotel_keywords"] = " ".join(authorized.pop("hotel_brands"))
        validate_conditions(authorized)
        staged = deepcopy(workflow)
        task = task_by_id(staged, task_id)
        task["conditions"].update({k: v for k, v in authorized.items() if k != "constraints"})
        if "constraints" in authorized:
            task["conditions"]["constraints"] = {
                **task["conditions"].get("constraints", {}),
                **authorized["constraints"],
            }
        rejected = update.get("rejected_candidate_ids", [])
        for domain in update["target"]["components"]:
            if domain in {"train", "hotel"}:
                if update["update_type"] == "replace":
                    selected = (task.get("draft_plan") or {}).get(domain + "_selection")
                    if selected:
                        rejected = [*rejected, selected["candidate_id"]]
                for row in staged["results_by_query"].values():
                    if row["task_id"] == task_id and row["domain"] == domain:
                        row["items"] = [i for i in row["items"] if i["id"] not in rejected]
        view = prepare_saved_trip_amendment(
            staged,
            {
                **context,
                "task_id": task_id,
                "evidence_max_age_seconds": self.limits.evidence_max_age_seconds,
            },
        )
        amendment_context = {
            **context,
            **view,
            "workflow": project_workflow_context(workflow),
            "authorized_changes": authorized,
            "amendment_target": deepcopy(update["target"]),
        }
        patch = await self.main.amend_saved_trip(amendment_context)
        if (
            patch.get("task_id") != task_id
            or set(patch.get("components", [])) != set(update["target"]["components"])
            or patch.get("condition_changes", {}) != authorized
        ):
            raise ValueError("amendment differs from authorized scope")
        for ref in patch.get("selection_refs", {}).values():
            if ref and ref.get("candidate_id") in rejected:
                raise ValueError("rejected candidate selected")
        return await self._amend_request(
            workflow, patch, {**context, "authorized_changes": authorized}, run
        )

    def _load_target(self, identity, context):
        if not self.memory:
            raise ValueError("storage required")
        w = self.memory.workflow_store.load(identity)
        observed = next(
            (r for r in context.get("known_workflows", []) if r["id"] == identity), None
        )
        if not w or not observed or observed["revision"] != w["revision"]:
            raise WorkflowConflictError("target revision changed")
        return w

    def _read_request(self, request, context):
        from agents.workflow_reads import read_saved_trip, render_saved_trip

        if not self.memory or not isinstance(request, dict) or not request.get("workflow_id"):
            return self._request_result(
                "needs_input", "请指定要查看的行程。", missing_fields=["workflow_id"]
            )
        workflow = self.memory.workflow_store.load(request["workflow_id"])
        if workflow is None:
            return self._request_result("partial", "未找到这份行程。", missing_fields=["workflow"])
        observed = next(
            (r for r in context.get("known_workflows", []) if r["id"] == workflow["id"]), None
        )
        expected = request.get(
            "expected_workflow_revision", observed["revision"] if observed else workflow["revision"]
        )
        if expected != workflow["revision"]:
            raise WorkflowConflictError("read target changed")
        result = read_saved_trip(workflow, {**request, "expected_workflow_revision": expected})
        current = self.memory.workflow_store.load(workflow["id"])
        if current is None or current["revision"] != expected:
            raise WorkflowConflictError("read target changed during pagination")
        return self._request_result(
            "ok" if not result["missing_fields"] else "partial",
            render_saved_trip(result),
            finalization_method="read",
            read_result=result,
            missing_fields=result["missing_fields"],
        )

    async def _amend_request(self, workflow, patch, context, run):
        from agents.workflow_amendments import apply_amendment_patch

        if workflow.get("policy_context"):
            from agents.policy_preflight import resolve_policy_for_workflow

            workflow = deepcopy(workflow)
            policy = resolve_policy_for_workflow(workflow["policy_context"], workflow, context)
            changed_scope = any(
                g.startswith("policy_applicability:")
                and context.get(g.split(":", 1)[1]) is not None
                for g in policy["missing_fields"]
            )
            if changed_scope:
                policy = await self._query_policy_context(context, run)
                policy = resolve_policy_for_workflow(policy, workflow, context)
            workflow["policy_context"] = policy

        result = apply_amendment_patch(workflow, patch, context)
        if result["validation"]["valid"]:
            self.w, self.run_state, self.context = result["candidate_workflow"], run, context
            run.workflow = self.w
            run.candidates = CandidateStore()
            run.candidates.restore(self.w.get("candidate_cache", {}))
            self._save()
            return self._finish_batch("validated", patch.get("reply_template"))
        if not result["query_requests"]:
            return self._request_result(
                "partial",
                "修改方案仍有冲突，原方案保留。",
                workflow=workflow,
                workflow_id=workflow["id"],
                unapplied_patch=patch,
                gaps=result["gaps"],
            )
        # Preserve the committed plan; stage only the requested amendment and execution evidence.
        working = result["candidate_workflow"]
        working["previous_committed_plan"] = deepcopy(workflow)
        working["checkpoint"] = {"kind": "amendment", "unapplied_patch": deepcopy(patch)}
        self.initial_query_plan = result["query_requests"]
        return await self.run(context, run, working)

    def _finish_batch(self, reason, template):
        from utils.workflow_reply import build_reply_facts, render_reply_template

        result = self._envelope(
            "completed" if self.w["status"] == "completed" else "partial", reason, ""
        )
        facts = build_reply_facts(result)
        try:
            result["final_answer"] = render_reply_template(template, facts)
            result["reply_style"] = "model"
        except (ValueError, TypeError, KeyError):
            result["final_answer"] = facts["fallback"]
            result["reply_style"] = "fallback"
        return result

    async def run_memory_request(self, context, run):
        from agents.memory_tools import parse_memory_command, apply_preference_updates, read_memory

        command = context.get("memory_command") or parse_memory_command(
            context.get("original_query", "")
        )
        if command is None:
            return None
        if command.get("history"):
            actual = read_memory(self.memory, command)
            return self._request_result("ok", "已读取保存的历史行程。", memory_result=actual)
        if command.get("missing_fields"):
            return {
                "status": "needs_input",
                "finalization_method": "direct",
                "final_answer": "请指定要修改的偏好ID和内容。",
                "missing_fields": command["missing_fields"],
                "results": [],
                "domain_results": {},
            }
        result = (
            apply_preference_updates(self.memory, command.get("updates", []))
            if command.get("updates")
            else {"status": "ok", "applied": []}
        )
        actual = read_memory(self.memory, {})
        run.effective_preferences = {**actual["legacy_preferences"], "memories": actual["memories"]}
        context["user_preferences"] = deepcopy(run.effective_preferences)
        if command.get("remaining_query") and result["status"] == "ok":
            context["original_query"] = command["remaining_query"]
            context["preference_update_result"] = result
            return None
        text = "\n".join(m["text"] for m in actual["memories"]) or "暂无已保存的出行偏好。"
        return {
            "status": result["status"],
            "finalization_method": "direct",
            "final_answer": ("偏好已保存。\n" if result["applied"] else "") + text
            if result["status"] == "ok"
            else "偏好未能完整保存，请核对目标或稍后重试。",
            "memory_result": result,
            "results": [],
            "domain_results": {},
            "missing_fields": [],
        }

    async def run(self, context, run, workflow):
        self.context, self.run_state, self.w = context, run, deepcopy(workflow)
        self.latest, self.last_message_id = None, None
        self.counts, self.seen = {}, set()
        self.main_calls = 0
        self.decisions = 0
        self.invalid_actions = 0
        run.workflow = self.w
        run.candidates = CandidateStore()
        run.candidates.restore(self.w.get("candidate_cache", {}))
        now = (
            datetime.fromisoformat(context["current_time"]) if context.get("current_time") else None
        )
        if now:
            for row in self.w["results_by_query"].values():
                scope = self.w.get("refresh_scope")
                if scope and (
                    row["task_id"] not in scope["task_ids"]
                    or row["domain"] not in scope["components"]
                ):
                    continue
                if self.w.get("previous_committed_plan"):
                    target = task_by_id(self.w, row["task_id"])
                    original = task_by_id(self.w["previous_committed_plan"], row["task_id"])
                    if target["revision"] == original["revision"] or row[
                        "domain"
                    ] not in target.get("update_scope", []):
                        continue
                if row["parameters"].get("search_kind") == "hotel_place" or not row.get(
                    "fetched_at"
                ):
                    continue
                if (
                    row.get("needs_revalidation")
                    or (now - datetime.fromisoformat(row["fetched_at"])).total_seconds()
                    > self.limits.evidence_max_age_seconds
                ):
                    row["needs_revalidation"] = True
                    from travel_data.candidates import query_cache_key

                    run.candidates.cache.pop(
                        query_cache_key(row["domain"], row["parameters"]), None
                    )
        run.workflow_tool_budget = len(self.w["tasks"]) * self.limits.tools_per_task
        try:
            return (
                await self._loop()
                if self._request_active
                else await asyncio.wait_for(self._loop(), timeout=self.limits.turn_timeout)
            )
        except asyncio.TimeoutError:
            return self._stop("partial", "turn_timeout", "本轮时间上限已到，已有结果已保留。")
        except ModelCallLimitExceeded:
            return self._stop(
                "partial", "main_model_call_limit", "本轮模型请求达到上限，已有草稿已保留。"
            )
        except WorkflowConflictError:
            return self._envelope(
                "error", "workflow_revision_conflict", "规划已被另一会话更新，请按最新版本继续。"
            )
        except OSError:
            return self._envelope(
                "error", "workflow_save_failed", "状态保存失败，已停止；不会自动重放外部查询。"
            )
        except ContextBudgetExceeded:
            self.w.update(status="partial", stop_reason="context_budget_exceeded")
            try:
                self._save()
            except (OSError, WorkflowConflictError):
                # Retain the in-memory evidence; never replay a provider request.
                self.run_state.workflow = self.w
            raise
        except Exception:
            return self._stop(
                "error", "workflow_execution_error", "本轮执行失败，已保留可保存的规划状态。"
            )

    def _save(self):
        self.w["candidate_cache"] = self.run_state.candidates.snapshot()
        if self.memory:
            checkpoint = self.w.get("checkpoint")
            if checkpoint:
                checkpoint["expected_workflow_revision"] = self.w["revision"] + 1
            previous = self.w.get("previous_committed_plan")
            if previous and self.w["status"] != "completed":
                durable = deepcopy(previous)
                durable["revision"] = self.w["revision"]
                staged = {
                    k: deepcopy(v) for k, v in self.w.items() if k != "previous_committed_plan"
                }
                durable["pending_amendment"] = staged
                durable["execution_plan"] = deepcopy(self.w.get("execution_plan", []))
                saved = self.memory.workflow_store.save(
                    durable, expected_revision=self.w["revision"]
                )
                self.w["revision"] = saved["revision"]
            else:
                self.w.pop("previous_committed_plan", None)
                self.w.pop("pending_amendment", None)
                self.w = self.memory.workflow_store.save(
                    self.w, expected_revision=self.w["revision"]
                )
            self.memory.set_active_workflow(self.w["id"])
        else:
            self.w["revision"] += 1
            if self.w.get("checkpoint"):
                self.w["checkpoint"]["expected_workflow_revision"] = self.w["revision"]
        self.run_state.workflow = self.w

    def _message(self, payload, *, task_id=None):
        task = task_by_id(self.w, task_id) if task_id else None
        message = {
            **deepcopy(payload),
            "schema_version": 1,
            "message_id": identifier("msg"),
            "workflow_id": self.w["id"],
            "turn_id": self.run_state.turn_id,
            "task_id": task_id,
            "task_revision": task["revision"] if task else None,
            "in_reply_to": self.last_message_id,
        }
        self.last_message_id = message["message_id"]
        if self.memory:
            self.memory._record_event(
                {
                    "turn_id": self.run_state.turn_id,
                    "scope": "run",
                    "type": "workflow_message",
                    "workflow_id": self.w["id"],
                    "task_id": task_id,
                    "content": message,
                }
            )
        return message

    def _model_context(self):
        task = task_by_id(self.w, self.w["current_task_id"])
        effective, sources = effective_conditions(
            self.w["confirmed_conditions"], task["conditions"], self.run_state.effective_preferences
        )
        sources.update(task.get("field_sources", {}))
        index = self.w["tasks"].index(task)
        previous = self.w["tasks"][index - 1] if index else None
        boundary = (
            check_task(self.w, previous["id"], previous.get("draft_plan"))["reconstructed_plan"][
                "schedule"
            ]
            if previous
            else {}
        )
        prepared = prepare_task(
            self.w, task["id"], self.context.get("current_time"), previous_boundary=boundary
        )
        effective, sources = prepared["effective_conditions"], prepared["field_sources"]
        public = {
            k: deepcopy(v)
            for k, v in self.w.items()
            if k not in {"candidate_cache", "results_by_query"}
        }
        if public.get("validation"):
            public["validation"] = {
                k: public["validation"][k]
                for k in ("valid", "issues", "validated_revisions", "signature")
            }
        latest = deepcopy(self.latest)
        if latest:
            latest["query_results"] = [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "task_id",
                        "task_revision",
                        "domain",
                        "result_revision",
                        "status",
                        "candidate_total",
                        "missing_fields",
                        "message",
                    )
                }
                for r in latest.get("query_results", [])
            ]
        base = {
            k: v
            for k, v in self.context.items()
            if k not in {"active_workflows", "known_workflows"}
        }
        overview = [
            {
                k: t.get(k)
                for k in (
                    "id",
                    "revision",
                    "status",
                    "origin",
                    "destination",
                    "purpose",
                    "requires_hotel",
                    "depends_on",
                    "summary",
                )
            }
            for t in self.w["tasks"]
        ]
        return {
            **base,
            "original_query": self.w["original_query"],
            "current_user_query": self.context["original_query"],
            "workflow": project_workflow_context(public),
            "workflow_overview": overview,
            "current_task": deepcopy(task),
            "confirmed_conditions": deepcopy(self.w["confirmed_conditions"]),
            "effective_conditions": effective,
            "field_sources": sources,
            "effective_preferences": deepcopy(self.run_state.effective_preferences),
            "preflight": prepared,
            "previous_boundary": boundary,
            "relevant_results": [
                project_domain_result(
                    r,
                    candidate_limit=self.run_state.limits.candidate_limit,
                    requirements=decision_requirements(self.context),
                )
                for r in query_views(
                    self.w, task["id"], limit=self.run_state.limits.candidate_limit
                )
            ],
            "latest_child_result": project_child_result(latest, include_candidates=False)
            if latest
            else None,
            "issues": deepcopy(task["issues"]),
            "resources": {
                "decisions_remaining": None
                if is_calibrating()
                else self._decision_limit() - getattr(self, "decisions", 0),
                "tools_remaining": self.run_state.workflow_tool_budget,
                "task_info_remaining": self.limits.info_executions_per_task
                - self.counts.get(task["id"], 0),
            },
        }

    def _delegation_context(self):
        view = project_delegation_context(self._model_context())
        return view

    def _decision_limit(self):
        return (
            self.limits.main_decisions_per_turn
            or len(self.w["tasks"]) * self.limits.main_steps_per_task + self.limits.main_extra_steps
        )

    def _default_queries(self):
        from datetime import date, timedelta

        requests = []
        previous = {}
        for task in self.w["tasks"]:
            if previous:
                prepared = prepare_task(
                    self.w, task["id"], self.context.get("current_time"), previous_boundary=previous
                )
            else:
                prepared = prepare_task(self.w, task["id"], self.context.get("current_time"))
            effective = prepared["effective_conditions"]
            start = (
                task["conditions"].get("check_in")
                or effective.get("arrival_date")
                or (
                    effective.get("departure_date")
                    if getattr(self.executor.providers.get("hotel_search"), "hotel_places", False)
                    else None
                )
            )
            if (
                task["requires_hotel"]
                and start
                and effective.get("nights")
                and not task["conditions"].get("check_out")
            ):
                task["conditions"].setdefault("check_in", start)
                task["conditions"]["check_out"] = (
                    date.fromisoformat(start) + timedelta(days=effective["nights"])
                ).isoformat()
                task["field_sources"].update(check_in="proposal", check_out="proposal")
            previous = {
                k: task["conditions"][k] for k in ("check_out",) if task["conditions"].get(k)
            }
            for query in prepared["query_requests"]:
                container = task.get("query_containers", {}).get(query["domain"])
                if container and container["status"] in {"ready", "failed"}:
                    continue
                query = deepcopy(query)
                if container and container.get("invalidation_reason") == "explicit_refresh":
                    query["parameters"]["refresh"] = True
                query.update(task_id=task["id"], task_revision=task["revision"])
                if query["domain"] == "train" and prepared["query_mode"] == "arrival":
                    query["name"] = "train_search_by_arrival"
                if query["domain"] == "hotel" and not getattr(
                    self.executor.providers.get("hotel_search"), "hotel_places", False
                ):
                    if not all(task["conditions"].get(k) for k in ("check_in", "check_out")):
                        continue
                    query["parameters"].update(
                        {k: task["conditions"].get(k) for k in ("check_in", "check_out")}
                    )
                requests.append(query)
        return requests

    async def _execute_queries(self, queries):
        from agents.workflow_batches import bind_query_plan
        from travel_data.batch_execution import execute_query_batch

        batch = bind_query_plan(queries, self.w, self.context)

        async def save():
            self.w = self.run_state.workflow
            self._save()
            self._persist_query_progress(self.run_state)

        return await execute_query_batch(batch, self.run_state, self.executor, after_tool=save)

    def _persist_query_progress(self, run):
        if not self.memory:
            return
        while run.tool_record_seq < len(run.tool_requests):
            row = run.tool_requests[run.tool_record_seq]
            self.memory.session_store.append_run(
                {
                    "type": "tool_execution",
                    "turn_id": run.turn_id,
                    "workflow_id": run.workflow["id"] if run.workflow else None,
                    **deepcopy(row),
                }
            )
            run.tool_record_seq += 1

    def _batch_context(self):
        rows = []
        for task in self.w["tasks"]:
            for row in query_views(self.w, task["id"], limit=self.run_state.limits.candidate_limit):
                rows.append(
                    project_domain_result(
                        row, candidate_limit=self.run_state.limits.candidate_limit
                    )
                )
        rows.extend(
            project_domain_result(row) for row in self.w.get("auxiliary_results", {}).values()
        )
        has_candidates = all(
            any(row.get("task_id") == task["id"] and row.get("domain") == domain
                and row.get("items") for row in rows)
            for task in self.w["tasks"]
            for domain in ["train", *(["hotel"] if task["requires_hotel"] else [])]
        )
        issues = self.w.get("validation", {}).get("issues", []) if self.w.get("validation") else []
        return {
            **{
                k: v
                for k, v in self.context.items()
                if k not in {"active_workflows", "known_workflows"}
            },
            "workflow": project_workflow_context(self.w),
            "task_results": rows,
            "allowed_actions": ["commit_batch"] if has_candidates and not issues else [
                "commit_batch", "schedule_queries", "repair_tasks", "ask_user"
            ],
            "query_tools": [
                deepcopy(schema["function"])
                for schema in self.executor.schemas()
                if schema["function"]["name"] in {
                    "train_search", "train_search_by_arrival", "hotel_search", "weather_query"
                }
            ],
            "preserved_selections": {
                t["id"]: {
                    d: (t.get("draft_plan") or {}).get(d + "_selection")
                    for d in ("train", "hotel")
                    if d not in t.get("update_scope", ["train", "hotel"])
                }
                for t in self.w["tasks"]
            },
            "policy_context": self.w.get("policy_context"),
            "issues": self.w.get("validation", {}).get("issues", [])
            if self.w.get("validation")
            else [],
            "reply_catalog": [
                f"s{i}_{field}"
                for i in range(1, len(self.w["tasks"]) + 1)
                for field in (
                    "route",
                    "train",
                    "departure",
                    "arrival",
                    "fare",
                    "stock",
                    "hotel",
                    "stay",
                    "hotel_cost",
                )
            ],
        }

    def _hotel_capability_gap_only(self, validation):
        """Stop only when every blocker is proven to be an unqueryable room quote."""
        if not getattr(self.executor.providers.get("hotel_search"), "hotel_places", False):
            return False
        affected = set()
        for task in self.w["tasks"]:
            effective, _ = effective_conditions(
                self.w.get("confirmed_conditions", {}), task["conditions"], {}
            )
            constraints = effective.get("constraints", {})
            if task["requires_hotel"] and (
                effective.get("hotel_quote_required")
                or any(k.startswith("hotel_max_") for k in constraints)
                or "total_budget_cny" in constraints
            ):
                affected.add(task["id"])
        fields = {
            rule.get("id"): rule.get("field")
            for rule in (self.w.get("policy_context") or {}).get("constraints", [])
        }
        blockers = [issue for issue in validation.get("issues", []) if issue.get("blocking")]
        for issue in blockers:
            task_ids = set(issue.get("task_ids", []))
            if issue["code"] in {"hotel_selection_missing", "hotel_price_unknown"}:
                if not task_ids or not task_ids <= affected:
                    return False
            elif issue["code"] == "policy_constraint_unverified":
                if (not task_ids or not task_ids <= affected
                        or fields.get(issue.get("evidence")) not in {
                            "hotel_max_nightly_cny", "hotel_max_total_cny"
                        }):
                    return False
            elif issue["code"] == "total_budget_unknown":
                missing = validation.get("budget", {}).get("missing_categories", [])
                if not missing or any(
                    row.get("category") != "hotel" or row.get("task_id") not in affected
                    for row in missing
                ):
                    return False
            else:
                return False
        return bool(blockers)

    def _hard_date_question(self):
        from agents.condition_conflicts import find_hard_date_conflict

        conflict = find_hard_date_conflict(self.w)
        if not conflict:
            return None
        self.w.update(status="running", stop_reason="user_input_required",
                      checkpoint={"kind": "hard_condition_conflict", **conflict})
        self._save()
        return self._envelope("needs_input", "user_input_required", conflict["question"])

    async def _loop(self):
        from agents.workflow_batches import initialize_query_containers, check_batch_submission
        from agents.workflow_state import STOPPED, derive_dispatchability

        initialize_query_containers(self.w)
        if self.w.get("stop_reason") in STOPPED:
            return self._stop("partial", self.w["stop_reason"], "规划已停止。")
        conflict = self._hard_date_question()
        if conflict:
            return conflict
        for task in self.w["tasks"]:
            guard = derive_dispatchability(self.w, task["id"], self.w.get("execution_plan", []))
            if guard["inflight"]:
                return self._stop(
                    "partial",
                    "request_completion_unknown",
                    "此前请求已开始但结果未确认，未重复查询。",
                )
            if "origin" in guard["missing_fields"]:
                self.w.update(
                    status="running",
                    stop_reason="user_input_required",
                    checkpoint={
                        "kind": "required_conditions",
                        "missing_fields": ["origin"],
                        "question": "你准备从哪个城市出发？",
                        "affected_task_ids": [task["id"]],
                        "resume_task_id": task["id"],
                    },
                )
                self._save()
                return self._envelope(
                    "needs_input", "user_input_required", "你准备从哪个城市出发？"
                )
        queries = getattr(self, "initial_query_plan", None)
        if queries is None:
            queries = self._default_queries()
        else:
            self._default_queries()  # prepare authorized date defaults and boundary suggestions
        if queries:
            await self._execute_queries(queries)
        if self.w.get("policy_context"):
            from agents.policy_preflight import resolve_policy_for_workflow

            self.w["policy_context"] = resolve_policy_for_workflow(
                self.w["policy_context"], self.w, self.context
            )
        self.initial_query_plan = None
        conflict = self._hard_date_question()
        if conflict:
            return conflict
        feedback, seen = None, set()
        for _ in range(self._decision_limit()):
            context = self._batch_context()
            context["action_feedback"] = feedback
            with model_scope(turn_id=self.run_state.turn_id, workflow_id=self.w["id"]):
                if hasattr(self.main, "select_batch"):
                    action = await self.main.select_batch(context)
                else:
                    context.update(self._model_context())
                    action = await self.main.step(context)
            self.main_calls += 1
            if not isinstance(action, dict):
                return self._stop("error", "invalid_workflow_action", "模型动作无效。")
            if self.memory:
                self.memory.session_store.append_run(
                    {
                        "type": "workflow_decision",
                        "turn_id": self.run_state.turn_id,
                        "workflow_id": self.w["id"],
                        "workflow_revision": self.w["revision"],
                        "action": action.get("action"),
                    }
                )
            signature = json.dumps(
                {
                    "action": action,
                    "tasks": [(t["id"], t["revision"], t["status"]) for t in self.w["tasks"]],
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            if signature in seen:
                return self._stop("partial", "no_progress", "重复决定没有改善方案，本轮已停止。")
            seen.add(signature)
            name = action.get("action")
            if name in {"schedule_queries", "dispatch", "repair_tasks", "ask_user"} and context["allowed_actions"] == ["commit_batch"]:
                feedback = {
                    "code": "commit_required",
                    "allowed_actions": ["commit_batch"],
                    "message": "已有可选候选，先提交整体草稿；程序校验实际冲突后再开放定向补查询。",
                }
                continue
            if name == "commit_batch":
                try:
                    committed = check_batch_submission(self.w, action)
                except (ValueError, KeyError, TypeError) as exc:
                    feedback = {"code": "invalid_batch", "message": str(exc)}
                    continue
                self.w = committed["workflow"]
                self.run_state.workflow = self.w
                self._save()
                if committed["validation"]["valid"]:
                    self.w.pop("previous_committed_plan", None)
                    return self._finish_batch("validated", action.get("reply_template"))
                feedback = committed["validation"]
                conflict = self._hard_date_question()
                if conflict:
                    return conflict
                if action.get("allow_repair") is not False:
                    if self._hotel_capability_gap_only(committed["validation"]):
                        return self._finish_batch(
                            "provider_capability_missing", action.get("reply_template")
                        )
                    continue
                return self._finish_batch("model_partial", action.get("reply_template"))
            if name in {"schedule_queries", "dispatch"}:
                queries = action.get("query_plan") or action.get("query_requests") or []
                if not queries or (
                    name == "dispatch"
                    and set(action)
                    - {"action", "task_id", "task_revision", "goal", "mode", "query_requests"}
                ):
                    feedback = {
                        "code": "invalid_workflow_action",
                        "message": "查询动作必须提供非空、可执行的 query_plan。",
                    }
                    continue
                if name == "dispatch":
                    task = task_by_id(self.w, action.get("task_id"))
                    queries = [
                        {**q, "task_id": task["id"], "task_revision": task["revision"]}
                        for q in queries
                    ]
                await self._execute_queries(queries)
                feedback = None
            elif name == "repair_tasks":
                self.w = apply_repair(self.w, action)
                self.run_state.workflow = self.w
                self._save()
                await self._execute_queries(self._default_queries())
                conflict = self._hard_date_question()
                if conflict:
                    return conflict
                feedback = None
            elif name == "ask_user":
                required = set(action.get("missing_fields", [])) & {
                    "origin",
                    "workflow_id",
                    "task_id",
                    "components",
                }
                fixed_conflict = any(
                    i["blocking"]
                    and i["code"]
                    not in {
                        "selection_not_ready",
                        "task_not_draft",
                        "invalid_draft",
                        "train_selection_missing",
                        "hotel_selection_missing",
                    }
                    for i in check_workflow(self.w)["issues"]
                )
                if (
                    not required
                    and not fixed_conflict
                    and any(t.get("draft_stage") == "query" for t in self.w["tasks"])
                ):
                    feedback = {
                        "code": "available_queries_required",
                        "message": "已有查询资料，请使用默认人数与未知预算形成方案，不追问可缺省字段。",
                    }
                    continue
                self.w.update(
                    status="running",
                    stop_reason="user_input_required",
                    checkpoint={
                        "question": action.get("question"),
                        "missing_fields": action.get("missing_fields", []),
                        "affected_task_ids": action.get("affected_task_ids", []),
                    },
                )
                self._save()
                return self._envelope(
                    "needs_input", "user_input_required", action.get("question", "请补充必要条件。")
                )
            elif name == "draft_task":
                task = task_by_id(self.w, action["task_id"])
                checked = check_batch_submission(
                    self.w,
                    {
                        "expected_workflow_revision": self.w["revision"],
                        "draft_tasks": [
                            {
                                "task_id": task["id"],
                                "task_revision": task["revision"],
                                "draft_plan": action["draft_plan"],
                            }
                        ],
                    },
                )
                self.w = checked["workflow"]
                self.run_state.workflow = self.w
                self._save()
                pending = next(
                    (t for t in self.w["tasks"] if t.get("draft_stage") != "selection"), None
                )
                if pending:
                    self.w["current_task_id"] = pending["id"]
            elif name in {"validate_workflow", "finish"}:
                checked = check_workflow(self.w)
                if checked["valid"]:
                    self.w = finalize_workflow(self.w, checked)
                    self._save()
                    return self._finish_batch("validated", action.get("reply_template"))
                feedback = checked
                if name == "finish" and action.get("status") == "partial":
                    if any(t.get("draft_stage") == "query" for t in self.w["tasks"]):
                        feedback = {
                            "code": "available_queries_required",
                            "message": "请先保留所有已取得资料的选择和缺口。",
                        }
                        continue
                    return self._stop("partial", "model_partial", "方案仍有未核实事项。")
            else:
                return self._stop(
                    "error", "invalid_workflow_action", "模型动作未启用，未继续查询。"
                )
        return self._stop("partial", "main_decision_limit", "本轮决策达到上限，已有资料保留。")

    def _stop(self, status, reason, answer):
        self.w.update(
            status="running" if status != "completed" else "completed", stop_reason=reason
        )
        try:
            self._save()
        except (OSError, SQLiteError, WorkflowConflictError):
            return self._envelope(
                "error", "workflow_save_failed", "状态保存失败，已停止；不会自动重放外部查询。"
            )
        return self._envelope(status, reason, answer)

    def _envelope(self, status, reason, answer, gaps=None):
        from agents.execution_harness import public_results

        checked = check_workflow(self.w)
        if reason in {"validated", "model_partial", "workflow_reused"}:
            # As in legacy forwarding, factual prose is rebuilt from trusted evidence.
            count = len(self.w["tasks"])
            answer = (
                f"火车与酒店安排已整理，共 {count} 段。"
                if status == "completed"
                else f"火车与酒店规划仍有待核实信息，已保留 {count} 段任务及已有草稿。"
            )
            budget = checked["budget"]
            answer += f"已核实费用合计 {budget['known_subtotal_cny']} 元。"
            if not budget["verified"]:
                answer += "酒店等未知费用另计，全程费用尚未完整核实。"
        simulated = any(
            str((row.get("source") or {}).get("provider", "")).startswith("simulation:")
            for row in self.w["results_by_query"].values()
        )
        if simulated:
            answer = "【模拟接口测评，车次、票价和酒店均非真实数据】" + answer
        if reason == "workflow_reused":
            answer = "已复用已有规划，保存的报价和库存并非实时信息。" + answer
        missing = sorted(
            {
                field
                for t in self.w["tasks"]
                for field in prepare_task(self.w, t["id"], self.context.get("current_time"))[
                    "missing_fields"
                ]
            }
        )
        visible = self.w
        if self.w.get("previous_committed_plan") and status != "completed":
            visible = deepcopy(self.w["previous_committed_plan"])
            visible["revision"] = self.w["revision"]
            answer += " 修改尚未通过，原方案保留。"
        return {
            "status": status,
            "finalization_method": "workflow",
            "final_answer": answer,
            "data_mode": "simulation" if simulated else "provider",
            "workflow_id": self.w["id"],
            "workflow_revision": self.w["revision"],
            "workflow": deepcopy(visible),
            "validated_plan": checked,
            "stop_reason": reason,
            "gaps": gaps or checked["issues"],
            "results": public_results(self.run_state.results),
            "domain_results": {},
            "missing_fields": missing,
        }
