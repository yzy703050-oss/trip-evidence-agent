import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture
from test_batched_intents import select_and_reply, single_trip
from test_lookup_planning import batch

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget
from trip_assistant.settings import Settings
from trip_assistant.storage.sessions import SessionStore
from trip_assistant.tools.registry import build_registry


def tool(identity, name, **args):
    return {"id": identity, "name": name, "arguments": {"request_kind": "planning", **args}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,data,allowed",
    [
        ("ok", {"status": "ok"}, True),
        ("partial", {"status": "no_knowledge"}, True),
        ("error", None, False),
        ("rejected", None, False),
        ("ok", None, False),
    ],
)
async def test_policy_observation_opens_business_only_for_returned_data(
    tmp_path, status, data, allowed
):
    from jsonschema import Draft202012Validator

    runtime = NativeRuntimeFixture(tmp_path)

    async def policy(args, turn):
        return {"status": status, **({"data": data} if data is not None else {})}

    runtime.registry["query_policy"] = replace(runtime.registry["query_policy"], handler=policy)
    runtime.model.script = [
        batch(("policy", "query_policy", {"request_kind": "planning"})),
        "政策结果已观察。",
    ]
    await runtime.run("单独测试政策前置观察")
    args = single_trip()
    args["tripplan"]["tasks"][0]["purpose"] = "business"
    for index, expected in [(0, False), (1, allowed)]:
        options = runtime.model.messages[index][1]
        schema = next(
            t["function"]["parameters"]
            for t in options["tools"]
            if t["function"]["name"] == "write_trip"
        )
        assert Draft202012Validator(schema).is_valid(args) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["lookup", "planning"])
async def test_policy_observation_keeps_only_declared_planning_context(tmp_path, request_kind):
    from trip_assistant.agents.prompts import MAIN_PROMPT, SUMMARY_PROMPT

    runtime = NativeRuntimeFixture(tmp_path)

    def observe(messages):
        expected = MAIN_PROMPT if request_kind == "planning" else SUMMARY_PROMPT
        assert messages[0]["content"] == expected
        return "政策库没有适用文件。"

    runtime.model.script = [
        batch(("policy", "query_policy", {"request_kind": request_kind})),
        observe,
    ]
    result = await runtime.run("测试政策上下文")
    assert result["final_answer"] == "政策库没有适用文件。"
    assert len(runtime.model.messages) == 2


@pytest.mark.asyncio
async def test_policy_stage_recovers_from_paired_receipt_without_requery(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator

    from trip_assistant.runtime.recovery import restore_turn

    class Crash(BaseException):
        pass

    runtime = NativeRuntimeFixture(tmp_path)
    checkpoint = runtime.sessions.checkpoint_turn

    def interrupt_after_policy(turn_id, patch):
        checkpoint(turn_id, patch)
        if patch.get("policy_context"):
            raise Crash()

    runtime.model.script = [batch(("p", "query_policy", {"request_kind": "planning"}))]
    with monkeypatch.context() as patch:
        patch.setattr(runtime.sessions, "checkpoint_turn", interrupt_after_policy)
        with pytest.raises(Crash):
            await runtime.run("安排出差")
    renewed = NativeRuntimeFixture(tmp_path)
    turn_id = renewed.sessions.read_state()["active_turn"]["turn_id"]
    restored = restore_turn(
        renewed.sessions, turn_id, settings=renewed.settings, trip_service=renewed.trip_service
    )
    assert restored["can_resume"]
    renewed.model.script = ["已观察上次政策结果。"]
    await renewed.run("安排出差", context=restored["context"], budget=restored["budget"])
    args = single_trip()
    args["tripplan"]["tasks"][0]["purpose"] = "business"
    schema = next(
        t["function"]["parameters"]
        for t in renewed.model.messages[0][1]["tools"]
        if t["function"]["name"] == "write_trip"
    )
    assert Draft202012Validator(schema).is_valid(args)
    policy_calls = [
        c
        for e in renewed.sessions.read_events(event_type="tool_call")
        for c in e["tool_calls"]
        if c["name"] == "query_policy"
    ]
    assert len(policy_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("others", [("query",), ("read",), ("query", "read")])
async def test_policy_and_all_independent_tools_overlap_and_results_pair(tmp_path, others):
    policy_started, others_started = asyncio.Event(), asyncio.Event()
    started = set()

    async def policy(args, turn):
        policy_started.set()
        await asyncio.wait_for(others_started.wait(), 1)
        return {"status": "ok"}

    def independent(name):
        async def run(args, turn):
            started.add(name)
            if started == set(others):
                others_started.set()
            await asyncio.wait_for(policy_started.wait(), 1)
            return {"status": "ok"}

        return run

    registry = build_registry({"query_policy": policy, **{n: independent(n) for n in others}})
    calls = [tool("policy", "query_policy")]
    calls += [
        tool(
            n,
            n,
            **(
                {"resource": "trip_summaries"}
                if n == "read"
                else {
                    "query_plan": [{"domain": "hotel", "parameters": {"city": "上海"}}],
                }
            ),
        )
        for n in others
    ]
    sessions = SessionStore(tmp_path, "a", "s")
    results = await execute_batch(
        calls,
        registry=registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=sessions,
    )
    assert all(r["status"] == "ok" for r in results), results
    assert [r["call_id"] for r in results] == [c["id"] for c in calls]
    assert not sessions.pending_tool_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["propose", "select", "amend"])
async def test_policy_and_trip_write_need_model_observation_before_any_effect(tmp_path, operation):
    effects = []

    async def handler(args, turn):
        effects.append(args)
        return {"status": "ok"}

    registry = build_registry({"query_policy": handler, "write_trip": handler})
    plan = (
        single_trip()["tripplan"]
        if operation == "propose"
        else {
            "operation": operation,
            "trip_plan_id": "known-trip",
            "expected_revision": 1,
        }
    )
    calls = [tool("policy", "query_policy"), tool("plan", "write_trip", tripplan=plan)]
    sessions = SessionStore(tmp_path, "a", "s")
    results = await execute_batch(
        calls,
        registry=registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=sessions,
    )
    assert not effects
    assert all(
        r["status"] == "rejected" and r["reason"] == "policy_observation_required" for r in results
    )
    assert not sessions.pending_tool_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "partial"])
async def test_failed_preference_write_blocks_dependent_policy(tmp_path, failure):
    effects = []

    async def memory(args, turn):
        return {"status": failure}

    async def policy(args, turn):
        effects.append("policy")
        return {"status": "ok"}

    sessions = SessionStore(tmp_path, "a", "s")
    results = await execute_batch(
        [
            tool("policy", "query_policy"),
            tool("memory", "write_memory", updates=[{"op": "add", "text": "优先上午出发"}]),
        ],
        registry=build_registry({"write_memory": memory, "query_policy": policy}),
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=sessions,
    )
    assert not effects and results[0]["reason"] == "preference_write_failed"
    assert not sessions.pending_tool_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["pause", "cancel"])
async def test_independent_lifecycle_stop_does_not_need_new_policy_observation(tmp_path, operation):
    effects = []

    async def handler(args, turn):
        effects.append(args)
        return {"status": "ok"}

    sessions = SessionStore(tmp_path, "a", "s")
    results = await execute_batch(
        [
            tool("policy", "query_policy"),
            tool(
                "stop",
                "write_trip",
                tripplan={
                    "operation": operation,
                    "trip_plan_id": "old-trip",
                    "expected_revision": 1,
                    "reason": "用户明确停止旧行程",
                },
            ),
        ],
        registry=build_registry({"query_policy": handler, "write_trip": handler}),
        turn={"turn_id": "t"},
        budget=TurnBudget(Settings(_env_file=None)),
        sessions=sessions,
    )
    assert len(effects) == 2 and all(r["status"] == "ok" for r in results)
    assert not sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_business_batches_queries_policy_model_then_plans_then_reviews(tmp_path):
    from trip_assistant.agents.policy import TravelPolicyAgent
    from trip_assistant.runtime.telemetry import current_model_context
    from trip_assistant.tools.policy import build_policy_handler

    class Retrieval:
        async def search(self, text, limit):
            return [{"id": "fixture-policy", "content": "员工火车只乘二等座。"}]

    hotel, train = SimulatedHotelProvider(), SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    runtime.registry["query_policy"] = replace(
        runtime.registry["query_policy"],
        handler=build_policy_handler(
            TravelPolicyAgent(runtime.model, Retrieval()), runtime.trip_service.store
        ),
    )
    stages = []

    def policy_extract(messages):
        stages.append(current_model_context()["stage"])
        return SimpleNamespace(
            text=json.dumps(
                {
                    "status": "ok",
                    "summary": "火车仅二等座",
                    "applicability": {},
                    "references": [],
                    "effective_date": None,
                    "missing_fields": [],
                    "conflicts": [],
                    "constraints": [
                        {
                            "id": "seat",
                            "rule": "火车只乘二等座",
                            "hard": True,
                            "applicability": {},
                            "field": "seat_class",
                            "operator": "==",
                            "value": "二等座",
                            "unit": None,
                            "evidence_ids": ["fixture-policy"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    def propose(messages):
        from trip_assistant.agents.prompts import MAIN_PROMPT

        assert messages[0]["content"] == MAIN_PROMPT
        stages.append(current_model_context()["stage"])
        payload = json.loads(messages[-1]["content"])
        assert payload["query_results"][0]["data"]
        policy_result = next(r for r in payload["observations"] if r["name"] == "query_policy")
        assert policy_result["data"]["constraints"][0]["value"] == "二等座"
        proposal = single_trip()
        task = proposal["tripplan"]["tasks"][0]
        task.update(purpose="business", requires_hotel=True)
        task["conditions"]["nights"] = 1
        task["items"][0]["constraints"] = {"seat_class": "二等座"}
        task["items"].append({"domain": "hotel", "parameters": {"keywords": "如家"}})
        return batch(("plan", "write_trip", proposal))

    def review(messages):
        stages.append(current_model_context()["stage"])
        payload = json.loads(messages[-1]["content"])
        assert payload["tripplan"]["policy_context"]["constraints"][0]["value"] == "二等座"
        assert payload["query_results"][0]["data"][0]["task_bindings"]
        chosen = select_and_reply(payload)
        args = chosen.content[0]["input"]
        view = payload["tripplan"]["candidates"][1]
        args["tripplan"]["tasks"][0]["selection_refs"]["hotel"] = {
            "query_id": view["query_id"],
            "result_revision": view["result_revision"],
            "candidate_id": view["items"][0]["id"],
        }
        args["final_answer"] = "出差可乘模拟车次SIM001二等座，住上海如家酒店的模拟候选。"
        return chosen

    runtime.model.script = [
        batch(
            (
                "hotels",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [
                        {
                            "domain": "hotel",
                            "parameters": {"city": "上海", "keywords": "如家"},
                        }
                    ],
                },
            ),
            ("policy", "query_policy", {"request_kind": "planning"}),
        ),
        policy_extract,
        propose,
        review,
    ]
    result = await runtime.run("查上海如家酒店，安排明天重庆去上海出差，只要单程住一晚")
    assert result["status"] == "completed" and result["trip"]["operation"] == "select"
    assert len(runtime.model.messages) == 4 and stages == [
        "agent:travel_policy",
        "agent:main",
        "agent:trip_review",
    ]
    assert len(hotel.calls) == 1 and len(train.calls) == 1
    assert not runtime.sessions.pending_tool_calls() and result["fact_issues"] == []
