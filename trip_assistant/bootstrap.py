"""Compose the one native runtime; imports do not initialize network or RAG clients."""

import asyncio
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from trip_assistant.agents.main import MainAgent
from trip_assistant.agents.policy import TravelPolicyAgent
from trip_assistant.agents.preference import (
    PreferenceAnalysisAgent,
    pending_analysis,
    save_preference_analysis,
)
from trip_assistant.llm.budget import BudgetedModel
from trip_assistant.llm.client import OpenAIChatModel
from trip_assistant.providers.rag import LazyPolicyRetriever
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.runtime.loop import run_turn
from trip_assistant.runtime.recovery import restore_turn
from trip_assistant.runtime.resilience import call_with_retry
from trip_assistant.runtime.scheduler import Scheduler
from trip_assistant.runtime.telemetry import MeteredModel, model_scope
from trip_assistant.settings import generation_options
from trip_assistant.storage.evidence import EvidenceStore
from trip_assistant.storage.preferences import LongTermMemory
from trip_assistant.storage.sessions import SessionStore
from trip_assistant.tools.memory_write import build_memory_writer
from trip_assistant.tools.policy import build_policy_handler
from trip_assistant.tools.query import QueryService
from trip_assistant.tools.read import build_read_handler
from trip_assistant.tools.registry import build_registry
from trip_assistant.tools.trip_write import build_trip_writer
from trip_assistant.trips.query_execution import TripQueryExecutor
from trip_assistant.trips.service import TripService

_active_budget = ContextVar("native_active_budget", default=None)


class ResilientModel:
    def __init__(self, model, scheduler):
        self.model = model
        self.scheduler = scheduler

    def __getattr__(self, name):
        return getattr(self.model, name)

    async def __call__(self, messages, **kwargs):
        budget = _active_budget.get() or TurnBudget(self.scheduler.settings)
        attempt = 0

        async def invoke():
            nonlocal attempt
            attempt += 1
            with model_scope(attempt=attempt):
                return await self.model(messages, **kwargs)

        with model_scope(logical_call_id="model_" + uuid4().hex):
            return await call_with_retry(
                invoke,
                endpoint="llm:completion",
                budget=budget,
                scheduler=self.scheduler,
            )


class Application:
    def refresh_memory_context(self, context):
        profile = self.preferences.data
        context["effective_preferences"] = self.preferences.get_preference()
        context["system_context"] = {
            "timezone": "Asia/Shanghai",
            "current_time": context["current_time"],
            "legacy_preferences": context["effective_preferences"],
            "memories": [
                m for m in profile.get("travel_memories", []) if m.get("status") == "active"
            ],
            "preference_summary": profile.get("preference_analysis", {}).get("summary", ""),
            "memory_revision": profile["_revision"],
        }
        today = datetime.fromisoformat(context["current_time"]).date()
        context["system_context"]["calendar"] = {
            name: (today + timedelta(days=offset)).isoformat()
            for offset, name in enumerate(("today", "tomorrow", "day_after_tomorrow"))
        }

    async def run(self, query, *, restored=None):
        if restored and restored.get("can_resume") is False:
            raise ValueError("stopped or unknown operation requires a new user turn")
        context = (restored or {}).get("context", {}).copy()
        context.setdefault("turn_id", "turn_" + uuid4().hex)
        active = self.sessions.read_state().get("active_turn", {})
        if self.sessions.pending_tool_calls() and active.get("turn_id"):
            self.restore(active["turn_id"])
        context.setdefault("candidate_views", active.get("candidate_views", {}))
        budget = (restored or {}).get("budget") or TurnBudget(self.settings)
        token = _active_budget.set(budget)
        try:
            with model_scope(turn_id=context["turn_id"]):
                context.update(
                    current_time=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    effective_preferences=self.preferences.get_preference(),
                )
                context["model_managed_activity_times"] = True
                context["bound_trip_replies"] = True
                if not self.settings.single_pass_planning:
                    await self.update_inferred_preferences(budget)
                self.refresh_memory_context(context)
                context["refresh_memory_context"] = self.refresh_memory_context
                result = await run_turn(
                    query,
                    model=self.main,
                    registry=self.registry,
                    context=context,
                    sessions=self.sessions,
                    budget=budget,
                )
                self.sessions.append_run(
                    {
                        "type": "query_run",
                        "turn_id": result["turn_id"],
                        "status": result["status"],
                        "stop_reason": result["stop_reason"],
                        "budget": result["budget"],
                    }
                )
                return result
        finally:
            _active_budget.reset(token)

    async def update_inferred_preferences(self, budget, *, explicit=False, operation_id=None):
        profile = self.preferences.data
        state = profile.get("preference_analysis", {})
        from trip_assistant.agents.preference import select_analysis_samples

        rows = (
            select_analysis_samples(self.trips.list_summaries())
            if explicit
            else pending_analysis(
                self.trips.list_summaries(), set(state.get("covered_trip_ids", []))
            )
        )
        if not rows:
            return {"status": "no_samples", "reason": "没有已确认的有效历史行程。"}
        try:
            async with asyncio.timeout(budget.remaining_seconds()):
                result = await self.preference_agent.analyze(
                    {
                        "history": rows,
                        "memories": profile.get("travel_memories", []),
                        "existing_summary": state.get("summary", ""),
                        "memory_revision": profile["_revision"],
                    }
                )
                return save_preference_analysis(
                    self.preferences,
                    result,
                    "user_requested"
                    if explicit
                    else "incremental"
                    if state.get("initial_summary_done")
                    else "bootstrap",
                    operation_id=operation_id,
                )
        except Exception as error:
            self.sessions.append_run(
                {"type": "preference_analysis_error", "error_type": type(error).__name__}
            )
            if explicit:
                raise
            return {"status": "error", "reason": "偏好总结失败，原记忆和覆盖集合已保留。"}

    def restore(self, turn_id):
        return restore_turn(
            self.sessions, turn_id, settings=self.settings, trip_service=self.trip_service
        )


def create_application(
    settings,
    *,
    model=None,
    providers=None,
    storage_root=None,
    user_id="default_user",
    session_id=None,
    shared_scheduler=None,
):
    app = Application()
    app.settings = settings
    app.sessions = SessionStore(storage_root or "data/memory", user_id, session_id or uuid4().hex)
    app.preferences = LongTermMemory(user_id, str(storage_root or "data/memory"))
    app.trips = app.preferences.workflow_store
    app.trips.trip_memory.repair(app.trips)
    app.evidence = EvidenceStore(app.preferences.storage)
    app.scheduler = Scheduler(settings, shared=shared_scheduler)
    app.scheduler.record = app.sessions.append_run
    app.raw_model = (
        model
        if model is not None
        else OpenAIChatModel(
            settings.llm_model,
            api_key=settings.llm_api_key or settings.deepseek_api_key,
            client_kwargs={"base_url": settings.llm_base_url, "timeout": settings.request_timeout},
            generate_kwargs=generation_options(
                settings.llm_model,
                max_tokens=settings.llm_max_tokens,
                thinking=None,
            ),
        )
    )
    app.model = ResilientModel(
        MeteredModel(
            app.raw_model,
            app.sessions.append_run,
            input_usd_per_million=settings.llm_input_usd_per_1m_tokens,
            output_usd_per_million=settings.llm_output_usd_per_1m_tokens,
        ),
        app.scheduler,
    )
    child_model = BudgetedModel(
        app.model,
        budget_tokens=settings.memory_context_budget_tokens,
        record=app.sessions.append_run,
    )
    app.main = MainAgent(app.model)
    app.preference_agent = PreferenceAnalysisAgent(child_model)

    async def policy_request(fetch):
        return await call_with_retry(
            fetch,
            endpoint="policy:retrieval",
            budget=_active_budget.get() or TurnBudget(settings),
            scheduler=app.scheduler,
        )

    app.policy_retriever = LazyPolicyRetriever(child_model, request=policy_request)
    app.policy_agent = TravelPolicyAgent(child_model, app.policy_retriever)
    if providers is None:
        from trip_assistant.providers.amap_hotel import AmapHotelProvider
        from trip_assistant.providers.juhe_train import JuheTrainProvider

        providers = {
            "train": JuheTrainProvider(settings.juhe_train_api_key),
            "hotel": AmapHotelProvider(settings.amap_api_key),
        }
    app.providers = providers
    app.queries = QueryService(providers, app.evidence, app.scheduler)
    app.trip_service = TripService(app.trips, app.queries, app.scheduler)

    async def reanalyze(turn):
        import re

        query = turn.get("original_query", "")
        if not re.search(r"分析|总结|重整|reanal|summari", query, re.I):
            raise ValueError("preference reanalysis requires explicit user request")
        return await app.update_inferred_preferences(
            turn["budget"], explicit=True, operation_id=turn.get("operation_id")
        )

    app.trip_queries = TripQueryExecutor(app.trip_service)
    app.registry = build_registry(
        {
            "read": build_read_handler(app.trips, app.evidence),
            "query": app.queries.handler,
            "write_memory": build_memory_writer(app.preferences, reanalyze=reanalyze),
            "write_trip": build_trip_writer(app.trip_service),
            "query_policy": build_policy_handler(app.policy_agent, app.trips),
        }
    )
    app.registry["query"] = replace(
        app.registry["query"], expand_queries=app.queries.expand_queries
    )
    app.registry["write_trip"] = replace(
        app.registry["write_trip"],
        expand_queries=app.trip_service.expand_queries,
        followup=app.trip_queries,
    )
    hotel = providers.get("hotel") or providers.get("hotel_search")
    if getattr(hotel, "hotel_places", False):
        for name in ("query", "write_trip"):
            app.registry[name] = replace(
                app.registry[name],
                capabilities="酒店仅提供hotel_place地点；不提供room_quotes房价或inventory空房数据。",
            )
    return app
