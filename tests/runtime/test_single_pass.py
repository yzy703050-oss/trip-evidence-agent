"""Explicit single-pass experiment remains available for historical replays."""

import json
from copy import deepcopy
from uuid import uuid4

import pytest
from support.native import NativeRuntimeFixture, ScriptedModel

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.llm.client import IncompleteModelResponse
from trip_assistant.runtime.recovery import restore_turn
from trip_assistant.settings import Settings


def call(plan=None, **changes):
    args = {
        "request_kind": "planning",
        "tripplan": plan or proposal(),
        "final_answer": (
            "按铁路约12小时估算：21日出发，22日至23日游玩，24日返程；住21日至24日三晚。"
        ),
        **changes,
    }
    return {"id": "plan_" + uuid4().hex, "name": "write_trip", "arguments": args}


def response(*calls):
    return {
        "content": [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
            for c in calls
        ]
    }


def proposal():
    return {
        "operation": "propose",
        "reason": "铁路耗时暂按约12小时估算，抵达当天不排景点，留两天游玩；不是查证车次。",
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "requires_hotel": True,
                "conditions": {
                    "departure_date": "2026-10-21",
                    "check_in": "2026-10-21",
                    "check_out": "2026-10-24",
                    "nights": 3,
                },
                "field_sources": {"departure_date": "user", "check_out": "proposal"},
                "condition_strengths": {"departure_date": "hard", "check_out": "soft"},
                "content": [
                    {
                        "date": "2026-10-22",
                        "time": "上午",
                        "purpose": "游览外滩",
                        "description": "沿江步行",
                        "source": "proposal",
                        "strength": "soft",
                    },
                    {
                        "date": "2026-10-23",
                        "time": "下午",
                        "purpose": "博物馆参观",
                        "description": "按开放情况安排",
                        "source": "proposal",
                        "strength": "soft",
                    },
                ],
                "items": [{"domain": "train"}, {"domain": "hotel"}],
            },
            {
                "origin": "上海",
                "destination": "重庆",
                "purpose": "return",
                "conditions": {"departure_date": "2026-10-24"},
                "field_sources": {"departure_date": "proposal"},
                "condition_strengths": {"departure_date": "soft"},
                "items": [{"domain": "train"}],
            },
        ],
    }


def runtime(tmp_path, **settings):
    return NativeRuntimeFixture(
        tmp_path,
        {"train": SimulatedTrainProvider(duration_minutes=720), "hotel": SimulatedHotelProvider()},
        settings=Settings(_env_file=None, single_pass_planning=True, **settings),
    )


@pytest.mark.asyncio
async def test_explicit_single_pass_one_decision_saves_estimate_and_queries_without_select(
    tmp_path,
):
    app = create_application(
        Settings(_env_file=None, single_pass_planning=True),
        model=ScriptedModel([response(call())]),
        storage_root=tmp_path,
        providers={"train": SimulatedTrainProvider(), "hotel": SimulatedHotelProvider()},
    )
    result = await app.run("后天从重庆去上海旅游")
    assert len(app.raw_model.messages) == 1
    assert result["status"] == "partial" and result["execution_facts"]["draft_saved"]
    assert result["execution_facts"]["saved"] is False
    assert result["execution_jobs"] and "估算" in result["final_answer"]
    saved = app.trips.load(result["trip"]["trip_plan_id"])
    assert not saved.get("committed")
    assert len(saved["tasks"][0]["content"]) == 2
    assert not any(t.get("draft_plan") for t in saved["tasks"])


@pytest.mark.asyncio
async def test_explicit_single_pass_skips_implicit_preference_model(tmp_path):
    app = create_application(
        Settings(_env_file=None, single_pass_planning=True),
        model=ScriptedModel([{"text": "你好"}]),
        storage_root=tmp_path,
        providers={},
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("one decision cannot run automatic preference analysis")

    app.update_inferred_preferences = forbidden
    result = await app.run("你好")
    assert result["final_answer"] == "你好" and len(app.raw_model.messages) == 1


@pytest.mark.asyncio
async def test_invalid_write_ends_once_without_displaying_false_success(tmp_path):
    r = runtime(tmp_path)
    bad = proposal()
    bad["tasks"][0]["conditions"]["surprise"] = 1
    r.model.script = [response(call(bad, final_answer="行程已保存并出票！"))]
    result = await r.run("后天出发")
    assert len(r.model.messages) == 1
    assert not r.memory.workflow_store.list_summaries()
    assert "出票" not in result["final_answer"]
    assert "未保存" in result["final_answer"] and result["status"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"text": ""}, IncompleteModelResponse("truncated", usage={})])
async def test_empty_or_truncated_response_does_not_generate_again(tmp_path, answer):
    r = runtime(tmp_path)

    def emit(_messages):
        if isinstance(answer, Exception):
            raise answer
        return answer

    r.model.script = [emit]
    result = await r.run("规划旅游")
    assert len(r.model.messages) == 1
    assert result["status"] == "partial" and not r.memory.workflow_store.list_summaries()


@pytest.mark.asyncio
async def test_single_pass_tools_cannot_delegate_policy_or_reanalyze(tmp_path):
    from trip_assistant.tools.registry import single_pass_registry

    r = runtime(tmp_path)
    offered = single_pass_registry(r.registry)
    assert "query_policy" not in offered and "request_replan" not in offered
    with pytest.raises(ValueError):
        offered["write_memory"].validate({"request_kind": "planning", "operation": "reanalyze"})
    r.model.script = [
        response(
            {"id": "policy", "name": "query_policy", "arguments": {"request_kind": "planning"}}
        )
    ]
    result = await r.run("安排出差")
    assert len(r.model.messages) == 1 and result["tool_results"][0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_later_turn_preloads_plan_and_amends_with_one_decision(tmp_path):
    r = runtime(tmp_path)
    r.model.script = [response(call())]
    first = await r.run("后天出发去上海玩")
    identity = first["trip"]["trip_plan_id"]
    saved = r.memory.workflow_store.load(identity)
    original = deepcopy(saved["tasks"][0]["content"])

    def amend(messages):
        data = json.loads(messages[-1]["content"])
        plan = data["current_plans"][0]
        assert plan["trip_plan_id"] == identity and plan["revision"] == saved["revision"]
        assert plan["tasks"][0]["content"] == original
        content = deepcopy(original)
        content[0]["description"] = "沿江慢走，减少步行距离"
        return response(
            call(
                {
                    "operation": "amend",
                    "trip_plan_id": identity,
                    "expected_revision": plan["revision"],
                    "tasks": [
                        {
                            "task_id": plan["tasks"][0]["id"],
                            "components": ["content"],
                            "content": content,
                        }
                    ],
                },
                final_answer="已将沿江活动改为慢走，其余安排保留。",
            )
        )

    r.model.script = [amend]
    second = await r.run("把刚才的沿江活动改成慢走")
    assert len(r.model.messages) == 2
    updated = r.memory.workflow_store.load(identity)
    assert updated["tasks"][0]["content"][0]["description"] == "沿江慢走，减少步行距离"
    assert second["execution_facts"]["draft_saved"]


@pytest.mark.asyncio
async def test_model_started_marker_prevents_regeneration_after_crash(tmp_path):
    r = runtime(tmp_path)
    r.sessions.checkpoint_turn(
        "crashed",
        {
            "status": "running",
            "query": "旅游",
            "single_pass_model_started": True,
            "single_pass_planning": True,
        },
    )
    restored = restore_turn(r.sessions, "crashed", settings=r.settings, trip_service=r.trip_service)
    result = await r.run("旅游", context=restored["context"], budget=restored["budget"])
    assert len(r.model.messages) == 0
    assert result["status"] == "partial" and "未获得" in result["final_answer"]


@pytest.mark.asyncio
async def test_preloaded_projection_never_invokes_compaction_model(tmp_path):
    r = runtime(tmp_path)
    for index in range(5):
        r.sessions.append(
            {
                "scope": "session",
                "type": "message",
                "turn_id": str(index),
                "role": "user",
                "content": "旧消息" * 5000,
            }
        )
        r.sessions.append(
            {
                "scope": "session",
                "type": "message",
                "turn_id": str(index),
                "role": "assistant",
                "content": "旧答复",
                "final": True,
            }
        )
    r.model.script = [{"text": "你好"}]
    result = await r.run("你好")
    assert len(r.model.messages) == 1 and result["final_answer"] == "你好"
    assert "旧消息" not in str(r.model.messages[0])


@pytest.mark.asyncio
async def test_single_pass_does_not_rewrite_activity_using_query_keywords(tmp_path):
    r = runtime(tmp_path)
    proposed = proposal()
    activity = proposed["tasks"][0]["content"][0]
    activity.update(
        date="2026-10-22",
        time="晚上",
        source="user",
        strength="hard",
        purpose="会议",
        description="开会",
    )
    r.model.script = [response(call(proposed))]
    result = await r.run(
        "明天上午开会，后天晚上开会", context={"current_time": "2026-10-19T10:00:00+08:00"}
    )
    assert result["execution_facts"]["draft_saved"]
    assert result["trip"]["tasks"][0]["content"][0]["time"] == "晚上"
    assert result["trip"]["tasks"][0]["content"][0]["date"] == "2026-10-22"


@pytest.mark.asyncio
async def test_recover_saved_decision_before_dispatch_does_not_call_model(tmp_path):
    r = runtime(tmp_path)
    decision = {"text": "", "tool_calls": [call()]}
    r.sessions.checkpoint_turn(
        "recover",
        {
            "status": "running",
            "query": "旅游",
            "single_pass_model_started": True,
            "single_pass_planning": True,
            "single_pass_decision": decision,
        },
    )
    restored = restore_turn(r.sessions, "recover", settings=r.settings, trip_service=r.trip_service)
    result = await r.run("旅游", context=restored["context"], budget=restored["budget"])
    assert len(r.model.messages) == 0 and result["execution_facts"]["draft_saved"]
    assert len(r.memory.workflow_store.list_summaries()) == 1


@pytest.mark.asyncio
async def test_resume_after_queries_does_not_repeat_model_or_suppliers(tmp_path):
    r = runtime(tmp_path)
    r.model.script = [response(call())]
    first = await r.run("后天出发旅游")
    calls = [len(p.calls) for p in r.queries.providers.values()]
    active = r.sessions.read_state()["active_turn"]
    # Simulate a crash before the final answer was recorded, after paired execution.
    active.update(status="running", execution_state={"phase": "deciding"})
    r.sessions.update_state({"active_turn": active})
    second = await r.run("后天出发旅游", context=active)
    assert len(r.model.messages) == 1
    assert [len(p.calls) for p in r.queries.providers.values()] == calls
    assert second["final_answer"] == first["final_answer"]


@pytest.mark.asyncio
async def test_two_plans_and_failed_independent_query_preserve_drafts(tmp_path):
    r = runtime(tmp_path)
    lookup = {
        "id": "weather",
        "name": "query",
        "arguments": {
            "request_kind": "planning",
            "query_plan": [{"domain": "weather", "parameters": {"city": "上海"}}],
        },
    }

    # Avoid network: a real query boundary returns a deterministic failure fixture.
    async def failed(_args, _turn):
        return {"status": "error", "reason": "模拟天气服务不可用", "data": []}

    from dataclasses import replace

    r.registry["query"] = replace(r.registry["query"], handler=failed)
    r.model.script = [response(call(), call(), lookup)]
    result = await r.run("比较两份上海旅游安排并查天气")
    assert len(r.model.messages) == 1 and result["status"] == "partial"
    assert len(r.memory.workflow_store.list_summaries()) == 2
    assert "天气" in result["final_answer"]
    assert len(result["tool_results"]) == 3


@pytest.mark.asyncio
async def test_preloaded_candidates_can_be_selected_but_wrong_versions_still_reject(tmp_path):
    r = runtime(tmp_path)
    r.model.script = [response(call())]
    first = await r.run("规划旅游")
    trip = r.memory.workflow_store.load(first["trip"]["trip_plan_id"])
    bad = {
        "operation": "amend",
        "trip_plan_id": trip["id"],
        "expected_revision": trip["revision"] - 1,
        "tasks": [],
    }
    r.model.script = [response(call(bad, final_answer="已保存错误版本。"))]
    second = await r.run("修改这份安排")
    assert len(r.model.messages) == 2 and "未保存" in second["final_answer"]
    assert r.memory.workflow_store.load(trip["id"])["revision"] == trip["revision"]


@pytest.mark.asyncio
async def test_missing_policy_is_reported_even_if_model_omits_it(tmp_path):
    r = runtime(tmp_path)
    plan = proposal()
    plan["tasks"][0]["purpose"] = "business"
    r.model.script = [response(call(plan))]
    result = await r.run("出差规划")
    assert "政策" in result["final_answer"] and "未核实" in result["final_answer"]


def test_single_pass_creation_avoids_duplicate_global_departure_dates():
    from trip_assistant.tools.registry import build_registry, single_pass_registry

    tool = single_pass_registry(build_registry({}))["write_trip"]
    schema = tool.schema["function"]["parameters"]
    shared = schema["$defs"]["SharedPlanConditions"]["properties"]
    assert "start_date" not in shared and "departure_date" not in shared
    assert {"end_date", "passengers", "guests", "constraints"} <= shared.keys()
    args = call()["arguments"]
    args["tripplan"]["confirmed_conditions"] = {
        "end_date": "2026-10-25",
        "constraints": {"total_budget_cny": 3000},
    }
    validated = tool.validate(args)
    assert validated["tripplan"]["confirmed_conditions"]["constraints"]["total_budget_cny"] == 3000
    args["tripplan"]["confirmed_conditions"]["start_date"] = "2026-10-21"
    with pytest.raises(ValueError):
        tool.validate(args)
    # The explicit legacy contract remains available for old callers.
    assert build_registry({})["write_trip"].validate(args)["tripplan"]["confirmed_conditions"][
        "start_date"
    ]
