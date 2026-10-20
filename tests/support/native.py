import asyncio
import inspect
from copy import deepcopy


class FakeClock:
    def __init__(self, now=0):
        self.now = now
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)


class ScriptedModel:
    def __init__(self, script):
        self.script = list(script)
        self.messages = []

    async def __call__(self, messages, **kwargs):
        self.messages.append((deepcopy(messages), deepcopy(kwargs)))
        value = self.script.pop(0)
        if callable(value):
            value = value(messages)
        return await value if inspect.isawaitable(value) else value


class NativeRuntimeFixture:
    """Real native loop, tools and SQLite stores; only model and vendors are scripted."""

    def __init__(self, root, providers=None, policy=None, settings=None):
        from dataclasses import replace

        from trip_assistant.agents.main import MainAgent
        from trip_assistant.runtime.scheduler import Scheduler
        from trip_assistant.settings import Settings
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

        self.settings = settings or Settings(_env_file=None)
        self.sessions = SessionStore(root, "a", "s")
        self.memory = LongTermMemory("a", str(root))
        self.scheduler = Scheduler(self.settings)
        self.evidence = EvidenceStore(self.memory.storage)
        self.queries = QueryService(providers or {}, self.evidence, self.scheduler)
        self.trip_service = TripService(self.memory.workflow_store, self.queries, self.scheduler)

        class NoPolicy:
            async def query(self, context):
                return {
                    "status": "no_knowledge",
                    "summary": "没有政策证据",
                    "constraints": [],
                    "missing_fields": ["policy"],
                    "references": [],
                }

        self.trip_queries = TripQueryExecutor(self.trip_service)
        self.registry = build_registry(
            {
                "read": build_read_handler(self.memory.workflow_store, self.evidence),
                "query": self.queries.handler,
                "write_memory": build_memory_writer(self.memory),
                "write_trip": build_trip_writer(self.trip_service),
                "query_policy": build_policy_handler(
                    policy or NoPolicy(), self.memory.workflow_store
                ),
            }
        )
        self.registry["query"] = replace(
            self.registry["query"], expand_queries=self.queries.expand_queries
        )
        self.registry["write_trip"] = replace(
            self.registry["write_trip"],
            expand_queries=self.trip_service.expand_queries,
            followup=self.trip_queries,
        )
        self.model = ScriptedModel([])
        self.main = MainAgent(self.model)

    async def run(self, query, **kwargs):
        from trip_assistant.runtime.limits import TurnBudget
        from trip_assistant.runtime.loop import run_turn

        return await run_turn(
            query,
            model=self.main,
            registry=self.registry,
            context=kwargs.pop("context", {}),
            sessions=self.sessions,
            budget=kwargs.pop("budget", TurnBudget(self.settings)),
            **kwargs,
        )


def make_application(user_id, session_id, storage_path="data/memory", **kwargs):
    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    return create_application(
        Settings(_env_file=None),
        model=ScriptedModel([]),
        providers={},
        storage_root=storage_path,
        user_id=user_id,
        session_id=session_id,
    )


def record_user(sessions, query):
    from uuid import uuid4

    turn_id = "turn_" + uuid4().hex
    sessions.append(
        {
            "type": "message",
            "scope": "session",
            "turn_id": turn_id,
            "role": "user",
            "content": query,
        }
    )
    return turn_id


def active_trips(app):
    return [
        row
        for identity in app.sessions.read_state().get("active_workflow_ids", [])
        if (row := app.trips.load(identity)) is not None
    ]


def trip_observation(messages):
    import json

    data = json.loads(messages[-1]["content"])
    return data.get("tripplan", data)


def sourced_trip_reply(messages):
    """Model double's complete prose, derived from the actual tool observation."""
    result = trip_observation(messages)
    lines = ["行程已保存。"]
    for plan in result["validation"]["reconstructed_tasks"]:
        train = plan.get("train") or {}
        hotel = plan.get("hotel") or {}
        schedule = plan.get("schedule") or {}
        if train:
            lines.append(
                " ".join(
                    str(train[k])
                    for k in (
                        "train_number",
                        "origin_station",
                        "destination_station",
                        "seat_class",
                        "departure_at",
                        "arrival_at",
                    )
                    if train.get(k)
                )
            )
            if train.get("price_cny") is not None:
                lines.append(str(train["price_cny"]) + "元。")
        if hotel:
            lines.append(
                hotel["hotel_name"]
                + "，入住"
                + schedule["check_in"]
                + "，离店"
                + schedule["check_out"]
                + "。"
            )
            if hotel.get("stay_total_cny") is not None:
                lines.append(str(hotel["stay_total_cny"]) + "元。")
    lines.append("酒店地点信息不代表报价或空房；房价、空房和退改规则需另行确认，尚未预订。")
    return "\n".join(lines)


async def write_then_query(service, tripplan, turn):
    """Service test setup invokes the separate query executor after the business write."""
    from trip_assistant.trips.query_execution import TripQueryExecutor

    written = await service.apply(tripplan, turn)
    if any(
        p["reason"] == "awaiting_query" for p in written.get("pending_queries", [])
    ) and not written.get("stop_planning"):
        result = await TripQueryExecutor(service).run(
            written["trip_plan_id"],
            written["revision"],
            {**turn, "operation_id": turn["operation_id"] + "-query"},
        )
        return result["trip"]
    return written
