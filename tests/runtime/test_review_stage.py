import json
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider


def native(identity, args, name="write_trip"):
    return SimpleNamespace(
        content=[{"type": "tool_use", "id": identity, "name": name, "input": args}]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_operation", [True, False])
async def test_trip_review_uses_compact_prompt_context_and_only_existing_trip_tools(
    tmp_path, explicit_operation
):
    from trip_assistant.agents.prompts import MAIN_PROMPT, TRIP_REVIEW_PROMPT

    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )

    def select(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["original_query"] == "明天重庆去上海，只安排单程火车"
        assert payload["previous_round_summary"]["summary"] == "采用重庆居住地，先查去程候选"
        assert not any(m.get("tool_calls") or m["role"] == "tool" for m in messages)
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        assert MAIN_PROMPT not in str(messages)
        assert "偏好测试" in str(messages)
        trip = payload["tripplan"]
        view = trip["candidates"][0]
        selected = native(
            "select",
            {
                "request_kind": "planning",
                "round_summary": "选择最早车次，票价已知",
                "tripplan": {
                    "operation": "select",
                    "trip_plan_id": trip["trip_plan_id"],
                    "expected_revision": trip["revision"],
                    "tasks": [
                        {
                            "task_id": trip["tasks"][0]["id"],
                            "selection_refs": {
                                "train": {
                                    "query_id": view["query_id"],
                                    "result_revision": view["result_revision"],
                                    "candidate_id": view["items"][0]["id"],
                                }
                            },
                        }
                    ],
                },
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "final_answer": "模拟数据：重庆到上海SIM001二等座，2026-10-17 "
                "08:00出发、10:00到达，200元。单程行程已保存，尚未预订。",
            },
        )
        if not explicit_operation:
            selected.content[0]["input"]["tripplan"].pop("operation")
        return selected

    runtime.model.script = [
        native(
            "propose",
            {
                "request_kind": "planning",
                "round_summary": "采用重庆居住地，先查去程候选",
                "tripplan": {
                    "operation": "propose",
                    "default_round_trip": False,
                    "tasks": [
                        {
                            "origin": "重庆",
                            "destination": "上海",
                            "requires_hotel": False,
                            "conditions": {"departure_date": "2026-10-17"},
                            "items": [{"domain": "train", "ranking": [{"field": "departure_at"}]}],
                        }
                    ],
                },
            },
        ),
        select,
    ]
    result = await runtime.run(
        "明天重庆去上海，只安排单程火车",
        context={
            "system_context": {"memories": [{"text": "偏好测试"}], "memory_revision": 1},
        },
    )
    assert result["status"] == "completed" and len(runtime.model.messages) == 2
    tools = runtime.model.messages[1][1]["tools"]
    shown = json.loads(runtime.model.messages[1][0][-1]["content"])["tripplan"]["candidates"]
    expected_tools = ["write_trip", "request_replan"] + (
        ["read"] if any(v.get("has_more") for v in shown) else []
    )
    assert [t["function"]["name"] for t in tools] == expected_tools
    definitions = tools[0]["function"]["parameters"]["$defs"]
    allowed = {
        v["properties"]["operation"]["const"]
        for v in definitions.values()
        if "const" in v.get("properties", {}).get("operation", {})
    }
    assert allowed == {"select", "amend"}
    assert result["finalization_method"] == "main_agent"
    from trip_assistant.memory.planning import review_payload

    history = review_payload(
        runtime.sessions,
        {
            "turn_id": result["turn_id"],
            "original_query": "查看调整",
        },
        result["trip"]["trip_plan_id"],
    )["adjustment_history"]
    assert history[-1]["operation"] == "select"


def one_way_plan():
    return {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-17"},
                "items": [{"domain": "train"}],
            }
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["read", "query", "write_memory", "query_policy", "propose"])
async def test_review_rejects_forbidden_actions_without_regaining_main_permissions(
    tmp_path, forbidden
):
    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

    train = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})

    def inspect(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        return "本轮未提交选择，已有查询草稿。"

    runtime.model.script = [
        native("p", {"request_kind": "planning", "tripplan": one_way_plan()}),
        native(
            "bad",
            {"request_kind": "planning", "tripplan": one_way_plan()},
            "write_trip" if forbidden == "propose" else forbidden,
        ),
        inspect,
    ]
    result = await runtime.run("规划重庆去上海单程行程")
    assert len(runtime.model.messages) == 3
    assert [t["function"]["name"] for t in runtime.model.messages[-1][1]["tools"]] == [
        "write_trip",
        "request_replan",
    ]
    assert result["tool_results"][1]["status"] == "rejected"
    assert result["stop_reason"] is None
    assert len(train.calls) == 1 and not runtime.memory.data.get("travel_memories")
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_offline_eval_reads_the_stage_projection(tmp_path):
    from evals.native_runner import run_case

    result = await run_case("default_date", SimpleNamespace(live_model=False, output=tmp_path))
    assert result["assessment"]["passed"]


@pytest.mark.asyncio
async def test_multiple_trip_results_use_summary_projection(tmp_path):
    from copy import deepcopy

    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    other = deepcopy(one_way_plan())
    other["tasks"][0]["origin"] = "北京"

    def inspect(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        assert len(payload["tripplans"]) == 2
        assert not any(m["role"] == "tool" for m in messages)
        return "两份行程候选已查询，尚未选择。"

    a = native("a", {"request_kind": "planning", "tripplan": one_way_plan()})
    b = native("b", {"request_kind": "planning", "tripplan": other})
    runtime.model.script = [
        SimpleNamespace(content=[a.content[0], b.content[0]]),
        inspect,
    ]
    result = await runtime.run("分别规划重庆到上海和北京到上海两次单程行程")
    assert len(runtime.model.messages) == 2
    assert len(result["tool_results"]) == 2
    assert len(result["execution_jobs"]) == 2
    assert result["stop_reason"] is None
    assert result["final_answer"] == "两份行程候选已查询，尚未选择。"


@pytest.mark.asyncio
async def test_review_cannot_change_another_existing_trip(tmp_path):
    from support.workflow import populated_workflow

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    other = runtime.trip_service.store.save(populated_workflow(quoted=True), expected_revision=None)
    runtime.model.script = [
        native("p", {"request_kind": "planning", "tripplan": one_way_plan()}),
        native(
            "other",
            {
                "request_kind": "planning",
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "tripplan": {
                    "operation": "amend",
                    "trip_plan_id": other["id"],
                    "expected_revision": other["revision"],
                    "confirmed_conditions": {"passengers": 2},
                },
            },
        ),
        "另一份行程不在本次评估范围，未修改。",
    ]
    result = await runtime.run("规划重庆到上海单程")
    assert result["tool_results"][1]["status"] == "rejected"
    assert result["tool_results"][1]["reason"] == "review_trip_mismatch"
    assert runtime.trip_service.store.load(other["id"]) == other


@pytest.mark.asyncio
async def test_real_adjustments_and_rejected_change_are_restored_from_shared_memory(tmp_path):
    from support.native import trip_observation

    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT
    from trip_assistant.memory.planning import review_payload
    from trip_assistant.storage.sessions import SessionStore

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    serial = 0

    def select(messages):
        nonlocal serial
        serial += 1
        trip = trip_observation(messages)
        view = trip["candidates"][0]
        train = view["items"][0]
        answer = (
            f"模拟数据：{train['train_number']} 二等座，{train['departure_at']}出发，"
            f"{train['arrival_at']}到达，200元。单程行程已保存，尚未预订。"
        )
        return native(
            "select" + str(serial),
            {
                "request_kind": "planning",
                "round_summary": "选择本轮候选并保存",
                "tripplan": {
                    "operation": "select",
                    "trip_plan_id": trip["trip_plan_id"],
                    "expected_revision": trip["revision"],
                    "tasks": [
                        {
                            "task_id": trip["tasks"][0]["id"],
                            "selection_refs": {
                                "train": {
                                    "query_id": view["query_id"],
                                    "result_revision": view["result_revision"],
                                    "candidate_id": train["id"],
                                }
                            },
                        }
                    ],
                },
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "final_answer": answer,
            },
        )

    runtime.model.script = [
        native(
            "initial",
            {
                "request_kind": "planning",
                "tripplan": one_way_plan(),
                "round_summary": "先安排17日去程",
            },
        ),
        select,
    ]
    result = await runtime.run(
        "规划10月17日重庆去上海单程，日期可调整", context={"token_counter": lambda _: 1000}
    )
    assert result["status"] == "completed"
    trip_id = result["trip"]["trip_plan_id"]
    for day in (18, 19):
        old = runtime.trip_service.store.load(trip_id)
        runtime.model.script = [
            native(
                "amend" + str(day),
                {
                    "request_kind": "planning",
                    "round_summary": f"用户把出发日调整到{day}日",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "amend",
                        "trip_plan_id": trip_id,
                        "expected_revision": old["revision"],
                        "reason": f"用户要求改到{day}日",
                        "tasks": [
                            {
                                "task_id": old["tasks"][0]["id"],
                                "conditions": {"departure_date": f"2026-10-{day}"},
                                "items": [{"domain": "train"}],
                            }
                        ],
                    },
                },
            ),
            select,
        ]
        result = await runtime.run(
            f"将出发日期改到2026-10-{day}", context={"token_counter": lambda _: 1000}
        )
        assert result["status"] == "completed"

    def explain(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        assert (
            json.loads(messages[-1]["content"])["tripplan"]["tasks"][0]["conditions"][
                "departure_date"
            ]
            == "2026-10-19"
        )
        return "之前先改到2026-10-18，再改为2026-10-19。本次版本已变化，修改未应用，保留当前方案。"

    runtime.model.script = [
        native(
            "stale",
            {
                "request_kind": "planning",
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "tripplan": {
                    "operation": "amend",
                    "trip_plan_id": trip_id,
                    "expected_revision": 1,
                    "reason": "试图使用旧版本修改",
                },
            },
        ),
        explain,
    ]
    result = await runtime.run("把行程再调整一下", context={"token_counter": lambda _: 1000})
    assert result["stop_reason"] is None
    assert result["finalization_method"] == "main_agent"
    restored = SessionStore(tmp_path, "a", "restored")
    payload = review_payload(restored, {"turn_id": "later", "original_query": "继续"}, trip_id)
    history = payload["adjustment_history"]
    assert [h["operation"] for h in history] == [
        "propose",
        "select",
        "amend",
        "select",
        "amend",
        "select",
        "amend",
    ]
    assert history[-1]["effect"] == "not_applied"
    assert history[2]["changes"]["tasks"][0]["conditions"]["departure_date"] == "2026-10-18"
    assert history[4]["changes"]["tasks"][0]["conditions"]["departure_date"] == "2026-10-19"
    assert payload["tripplan"]["tasks"][0]["conditions"]["departure_date"] == "2026-10-19"


@pytest.mark.asyncio
async def test_restore_after_proposal_enters_review_from_existing_log(tmp_path):
    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    query = "规划2026-10-17重庆去上海单程"
    turn = {"turn_id": "interrupted", "original_query": query, "session_id": "s"}
    budget = TurnBudget(runtime.settings)
    turn["budget"] = budget
    runtime.sessions.append(
        {
            "event_id": "user:interrupted",
            "type": "message",
            "role": "user",
            "scope": "session",
            "turn_id": "interrupted",
            "content": query,
        }
    )
    await execute_batch(
        [
            {
                "id": "p",
                "name": "write_trip",
                "arguments": {
                    "request_kind": "planning",
                    "tripplan": one_way_plan(),
                    "round_summary": "候选已查到，下一轮选择",
                },
            },
        ],
        registry=runtime.registry,
        turn=turn,
        budget=budget,
        sessions=runtime.sessions,
    )
    runtime.sessions.checkpoint_turn(
        "interrupted", {**budget.checkpoint(), "query": query, "status": "running"}
    )
    restored = restore_turn(
        runtime.sessions,
        "interrupted",
        settings=runtime.settings,
        trip_service=runtime.trip_service,
    )

    def select(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        assert payload["previous_round_summary"]["summary"] == "候选已查到，下一轮选择"
        trip = payload["tripplan"]
        view = trip["candidates"][0]
        candidate = view["items"][0]
        return native(
            "selected",
            {
                "request_kind": "planning",
                "tripplan": {
                    "operation": "select",
                    "trip_plan_id": trip["trip_plan_id"],
                    "expected_revision": trip["revision"],
                    "tasks": [
                        {
                            "task_id": trip["tasks"][0]["id"],
                            "selection_refs": {
                                "train": {
                                    "query_id": view["query_id"],
                                    "result_revision": view["result_revision"],
                                    "candidate_id": candidate["id"],
                                }
                            },
                        }
                    ],
                },
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "final_answer": f"模拟数据：重庆到上海{candidate['train_number']}二等座，"
                f"{candidate['departure_at']}出发，{candidate['arrival_at']}抵达，200元。已保存，尚未预订。",
            },
        )

    runtime.model.script = [select]
    result = await runtime.run(query, context=restored["context"], budget=restored["budget"])
    assert restored["can_resume"] and result["status"] == "completed"
    assert len(runtime.model.messages) == 1 and result["budget"]["planning_rounds_used"] == 2
    assert not runtime.sessions.pending_tool_calls()
