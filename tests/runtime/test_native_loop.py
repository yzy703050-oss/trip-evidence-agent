import json
from datetime import UTC
from types import SimpleNamespace

import pytest
from support.native import ScriptedModel, sourced_trip_reply, trip_observation


def tool_response(*calls, text=""):
    return SimpleNamespace(
        content=([{"type": "text", "text": text}] if text else [])
        + [
            {"type": "tool_use", "id": identity, "name": name, "input": args}
            for identity, name, args in calls
        ]
    )


def call(identity, name, **args):
    return (identity, name, {"request_kind": "lookup", **args})


@pytest.fixture
def runtime(tmp_path):
    from trip_assistant.agents.main import MainAgent
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.loop import run_turn
    from trip_assistant.settings import Settings
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.storage.sessions import SessionStore
    from trip_assistant.tools.memory_write import build_memory_writer
    from trip_assistant.tools.read import build_read_handler
    from trip_assistant.tools.registry import build_registry

    sessions = SessionStore(tmp_path, "a", "s")
    memory = LongTermMemory("a", str(tmp_path))
    registry = build_registry(
        {
            "read": build_read_handler(memory.workflow_store, None),
            "write_memory": build_memory_writer(memory),
        }
    )
    scripted = ScriptedModel([])

    async def run(query, **extra):
        return await run_turn(
            query,
            model=MainAgent(scripted),
            registry=registry,
            context=extra.pop("context", {}),
            sessions=sessions,
            budget=extra.pop("budget", TurnBudget(Settings())),
            **extra,
        )

    return SimpleNamespace(
        run=run, model=scripted, sessions=sessions, memory=memory, registry=registry
    )


@pytest.mark.asyncio
async def test_plain_question_only_one_main_response(runtime):
    runtime.model.script = ["你好"]
    result = await runtime.run("你好")
    assert result["final_answer"] == "你好" and len(runtime.model.messages) == 1
    assert runtime.sessions.read_events(event_type="message", role="assistant")[-1]["final"]


@pytest.mark.asyncio
async def test_simple_lookup_result_always_returns_to_main(runtime):
    runtime.model.script = [
        tool_response(call("r", "read", resource="trip_summaries"), text="先查看行程摘要"),
        "你还没有保存行程。",
    ]
    result = await runtime.run("查一下我的行程")
    assert len(runtime.model.messages) == 2 and result["final_answer"] == "你还没有保存行程。"
    messages = runtime.model.messages[1][0]
    payload = json.loads(messages[-1]["content"])
    assert payload["observations"][0]["call_id"] == "r"
    assert payload["previous_round_summary"]["summary"] == "先查看行程摘要"
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_memory_write_and_trip_read_results_both_return_to_main(runtime):
    runtime.model.script = [
        tool_response(
            call("w", "write_memory", updates=[{"op": "add", "text": "出差尽量高铁"}]),
            call("r", "read", resource="trip_summaries"),
        ),
        "已记下：出差尽量高铁。",
    ]
    result = await runtime.run("记住出差尽量高铁，顺便查看偏好")
    payload = json.loads(runtime.model.messages[1][0][-1]["content"])
    assert payload["memory_results"][0]["status"] == "ok"
    assert payload["observations"][0]["data"]["summaries"] == []
    assert result["final_answer"].startswith("已记下")
    assert runtime.memory.data["travel_memories"][0]["text"] == "出差尽量高铁"


@pytest.mark.asyncio
async def test_json_in_text_does_not_drive_execution(runtime):
    runtime.model.script = [
        '{"response_mode":"dispatch","agent_schedule":[{"agent_name":"information_query"}]}'
    ]
    result = await runtime.run("测试文本")
    assert result["finalization_method"] == "main_agent"
    assert not [e for e in runtime.sessions.read_events() if e["type"] == "tool_call"]
    assert len(runtime.model.messages) == 1


@pytest.mark.asyncio
async def test_tool_error_returns_to_main_for_explanation(runtime):
    runtime.model.script = [
        tool_response(call("bad", "read", resource="trip", trip_plan_id="missing")),
        "没有找到这次行程，请提供行程 ID。",
    ]
    result = await runtime.run("查看行程")
    assert len(runtime.model.messages) == 2 and result["final_answer"].startswith("没有找到")
    assert (
        json.loads(runtime.model.messages[1][0][-1]["content"])["observations"][0]["status"]
        == "error"
    )


@pytest.mark.asyncio
async def test_third_planning_round_allows_final_observation_not_fourth_batch(runtime):
    from dataclasses import replace

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings

    budget = TurnBudget(Settings(_env_file=None, planning_rounds=3))
    calls = []

    async def write(args, turn):
        calls.append(args)
        return {"status": "partial", "reason": "need choice"}

    # Same validated argument contract, classified as complex write to exercise shared loop budget.
    runtime.registry["write_trip"] = replace(
        runtime.registry["read"], name="write_trip", category="complex_write", handler=write
    )
    runtime.model.script = (
        [tool_response(call("prep", "read", resource="trip_summaries"))]
        + [tool_response(call(f"p{i}", "write_trip", resource="trip_summaries")) for i in range(3)]
        + ["三轮后仍需确认。"]
    )
    result = await runtime.run("规划行程", budget=budget)
    assert len(calls) == 3 and result["budget"]["planning_rounds_used"] == 3
    assert runtime.model.messages[-1][1] == {"tool_choice": "none"}
    summary = json.loads(runtime.model.messages[-1][0][-1]["content"])["previous_round_summary"]
    assert summary["results"][0]["reason"] == "need choice"
    assert result["stop_reason"] == "planning_round_limit_exceeded"
    assert result["final_answer"] == "三轮后仍需确认。"


@pytest.mark.asyncio
async def test_total_deadline_includes_model_time(runtime):
    from support.native import FakeClock

    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.settings import Settings

    clock = FakeClock()

    async def slow(messages, **kwargs):
        clock.advance(601)
        return "too late"

    from trip_assistant.agents.main import MainAgent
    from trip_assistant.runtime.loop import run_turn

    result = await run_turn(
        "问题",
        model=MainAgent(slow),
        registry=runtime.registry,
        context={},
        sessions=runtime.sessions,
        budget=TurnBudget(Settings(), clock=clock),
    )
    assert (
        result["stop_reason"] == "turn_deadline_exceeded"
        and "too late" not in result["final_answer"]
    )


@pytest.mark.asyncio
async def test_business_policy_trip_candidates_selection_all_observed_by_main(tmp_path):
    from copy import deepcopy
    from datetime import datetime

    from support.native import NativeRuntimeFixture
    from support.workflow import populated_workflow

    from trip_assistant.trips.evidence import as_result

    sample = next(
        r
        for r in populated_workflow(quoted=True)["results_by_query"].values()
        if r["domain"] == "train"
    )

    class Provider:
        async def search(self, q):
            row = deepcopy(sample)
            row["parameters"] = q.to_dict()
            row["fetched_at"] = datetime.now(UTC).isoformat()
            row["source"]["fetched_at"] = row["fetched_at"]
            return as_result(row)

    class Policy:
        async def query(self, context):
            return {
                "status": "ok",
                "summary": "已核实适用政策",
                "constraints": [],
                "references": [{"id": "p"}],
                "applicability": {},
                "missing_fields": [],
                "conflicts": [],
                "effective_date": None,
            }

    runtime = NativeRuntimeFixture(tmp_path, {"train": Provider()}, Policy())

    def select(messages):
        result = trip_observation(messages)
        view = result["candidates"][0]
        assert result["tasks"][0]["purpose"] == "business"
        return tool_response(
            (
                "select",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "select",
                        "trip_plan_id": result["trip_plan_id"],
                        "expected_revision": result["revision"],
                        "tasks": [
                            {
                                "task_id": result["tasks"][0]["id"],
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
                },
            )
        )

    runtime.model.script = [
        tool_response(("policy", "query_policy", {"request_kind": "planning", "context": {}})),
        tool_response(
            (
                "plan",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": {
                        "tasks": [
                            {
                                "origin": "上海",
                                "destination": "北京",
                                "purpose": "business",
                                "conditions": {"departure_date": "2026-10-11"},
                                "items": [
                                    {
                                        "domain": "train",
                                        "parameters": {"departure_date": "2026-10-11"},
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        ),
        select,
        sourced_trip_reply,
    ]
    result = await runtime.run("单程去北京出差")
    assert result["status"] == "completed" and result["trip"]["status"] == "completed"
    assert result["budget"]["planning_rounds_used"] == 2 and len(runtime.model.messages) == 4
    assert result["final_answer"] == sourced_trip_reply(runtime.model.messages[-1][0])
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_lookup_external_query_is_observed_before_main_final(tmp_path):
    from support.native import NativeRuntimeFixture

    from trip_assistant.providers.contracts import AgentDataResult

    class Public:
        async def weather(self, city, day):
            from datetime import datetime

            from trip_assistant.providers.contracts import Source

            source = Source("simulation:weather", datetime.now(UTC), "https://weather.example.test")
            return AgentDataResult(
                "ok",
                {"city": city},
                [
                    {
                        "city": city,
                        "date": day,
                        "temp_c": 20,
                        "description": "晴",
                        "source": source.to_dict(),
                    }
                ],
                [],
                source,
                source.fetched_at,
                None,
            )

    runtime = NativeRuntimeFixture(tmp_path, {"public": Public()})
    runtime.model.script = [
        tool_response(
            call(
                "weather",
                "query",
                query_plan=[{"domain": "weather", "parameters": {"city": "上海"}}],
            )
        ),
        "上海天气查询结果为晴。",
    ]
    result = await runtime.run("上海天气")
    assert len(runtime.model.messages) == 2 and result["final_answer"] == "上海天气查询结果为晴。"
    assert (
        json.loads(runtime.model.messages[1][0][-1]["content"])["query_results"][0]["status"]
        == "ok"
    )


@pytest.mark.asyncio
async def test_output_triggered_compaction_reuses_main_response(runtime):
    for role in ("user", "assistant"):
        runtime.sessions.append(
            {
                "scope": "session",
                "type": "message",
                "turn_id": "old",
                "role": role,
                "content": "x" * 6500,
                "final": role == "assistant",
            }
        )
    summaries = []

    async def summarize(previous, events):
        summaries.append(events)
        return "旧目标与事实摘要。"

    def count(payload):
        return (
            sum(len(str(m.get("content") or "")) for m in payload["messages"])
            + len(payload.get("tools", [])) * 100
        )

    runtime.model.script = ["答" * 5000]
    result = await runtime.run("继续", context={"summarizer": summarize, "token_counter": count})
    assert len(runtime.model.messages) == 1 and len(summaries) == 1
    assert (
        result["final_answer"] == "答" * 5000
        and runtime.sessions.read_state()["covered_through_seq"] == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("destination_count", [1, 2, 4])
async def test_native_multi_city_and_return_use_model_stays_and_save_all_segments(
    tmp_path, destination_count
):
    from datetime import date, timedelta

    from support.native import NativeRuntimeFixture

    from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider

    train = SimulatedTrainProvider()
    hotel = SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    cities = ["上海", "杭州", "南京", "苏州"][:destination_count]
    route = ["北京", *cities, "北京"]
    tasks = []
    for i, (origin, destination) in enumerate(zip(route, route[1:], strict=False)):
        day = date(2026, 10, 20) + timedelta(days=2 * i)
        lodging = i < destination_count
        conditions = {"departure_date": day.isoformat()}
        if lodging:
            conditions.update(
                check_in=day.isoformat(), check_out=(day + timedelta(days=2)).isoformat(), nights=2
            )
        tasks.append(
            {
                "origin": origin,
                "destination": destination,
                "requires_hotel": lodging,
                "conditions": conditions,
                "items": [
                    {"domain": "train", "parameters": {}},
                    *([{"domain": "hotel", "parameters": {}}] if lodging else []),
                ],
            }
        )

    def select(messages):
        result = trip_observation(messages)
        assert len(result["tasks"]) == destination_count + 1
        assert result["status"] == "running" and result["committed_revision"] is None
        updates = []
        for task in result["tasks"]:
            refs = {}
            for view in result["candidates"]:
                if view["query_id"].startswith(task["id"] + ":"):
                    refs[view["domain"]] = {
                        "query_id": view["query_id"],
                        "result_revision": view["result_revision"],
                        "candidate_id": view["items"][0]["id"],
                    }
            assert set(refs) == ({"train", "hotel"} if task["requires_hotel"] else {"train"})
            updates.append({"task_id": task["id"], "selection_refs": refs})
        return tool_response(
            (
                "select",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "select",
                        "trip_plan_id": result["trip_plan_id"],
                        "expected_revision": result["revision"],
                        "tasks": updates,
                    },
                },
            )
        )

    runtime.model.script = [
        tool_response(
            ("plan", "write_trip", {"request_kind": "planning", "tripplan": {"tasks": tasks}})
        ),
        select,
        sourced_trip_reply,
    ]
    result = await runtime.run("从北京出发，按顺序游玩后返回北京，交通住宿都安排")
    assert result["status"] == "completed" and result["budget"]["planning_rounds_used"] == 2, (
        result["stop_reason"],
        result["fact_issues"],
        result["final_answer"],
    )
    assert len(train.calls) == destination_count + 1 and len(hotel.calls) == destination_count
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert saved["committed"] and saved["working"] is None
    assert all(t["status"] == "validated" for t in saved["tasks"])
    assert [t["conditions"].get("nights") for t in saved["tasks"][:-1]] == [2] * destination_count
    assert not saved["tasks"][-1]["requires_hotel"]
    assert result["finalization_method"] == "main_agent"


@pytest.mark.asyncio
async def test_final_event_keeps_trip_reference_without_full_snapshot(tmp_path):
    from support.native import NativeRuntimeFixture

    runtime = NativeRuntimeFixture(tmp_path)
    runtime.model.script = [
        tool_response(
            (
                "draft",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": {"tasks": [{"origin": "上海", "destination": "北京"}]},
                },
            )
        ),
        "需要补充出发日期，草稿已保留。",
    ]
    result = await runtime.run("单程去北京")
    event = runtime.sessions.read_events(event_type="message", role="assistant")[-1]
    assert event["trip_plan_id"] == result["trip"]["trip_plan_id"]
    assert event["trip_revision"] == result["trip"]["revision"]
    assert event["content"] == result["final_answer"]
    assert "tasks" not in event and "validation" not in event and "results_by_query" not in event


@pytest.mark.asyncio
async def test_successful_trip_does_not_hide_failed_query_in_mixed_request(tmp_path):
    from support.native import NativeRuntimeFixture
    from support.workflow import populated_workflow

    from trip_assistant.providers.contracts import AgentDataResult
    from trip_assistant.trips.validation import check_workflow, finalize_workflow

    class Public:
        async def weather(self, city, day):
            return AgentDataResult("unavailable", {"city": city}, [], [], None, None, "接口不可用")

    runtime = NativeRuntimeFixture(tmp_path, {"public": Public()})
    work = populated_workflow(quoted=True)
    work["user_id"] = "a"
    saved = runtime.trip_service.store.commit(
        finalize_workflow(work, check_workflow(work)), expected_revision=None, operation_id="seed"
    )
    runtime.model.script = [
        tool_response(
            (
                "weather",
                "query",
                {
                    "request_kind": "planning",
                    "query_plan": [{"domain": "weather", "parameters": {"city": "北京"}}],
                },
            )
        ),
        tool_response(
            (
                "confirm",
                "write_trip",
                {
                    "request_kind": "planning",
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "tripplan": {
                        "operation": "confirm",
                        "trip_plan_id": saved["id"],
                        "expected_revision": saved["revision"],
                    },
                },
            )
        ),
        "行程确认已保存；天气查询失败。",
    ]
    result = await runtime.run("确认采用这个行程，同时查北京天气")
    assert result["trip"]["status"] == "completed" and result["status"] == "partial"
