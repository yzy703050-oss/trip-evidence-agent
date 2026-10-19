import json

import pytest
from support.native import NativeRuntimeFixture
from test_lookup_planning import batch

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider


def single_trip():
    return {
        "request_kind": "planning",
        "tripplan": {
            "operation": "propose",
            "default_round_trip": False,
            "tasks": [
                {
                    "origin": "重庆",
                    "destination": "上海",
                    "conditions": {"departure_date": "2026-10-17"},
                    "items": [{"domain": "train"}],
                }
            ],
        },
    }


def select_and_reply(payload):
    trip = payload["tripplan"]
    view = trip["candidates"][0]
    return batch(
        (
            "select",
            "write_trip",
            {
                "request_kind": "planning",
                "goal_assessment": {
                    "status": "satisfied",
                    "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                },
                "final_answer": (
                    "北京如家酒店和杭州汉庭酒店的模拟候选已整理好。去上海可以乘模拟车次SIM001。"
                ),
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
                                },
                            },
                        }
                    ],
                },
            },
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", ["bound", "lookup", "multiple", "weather", "memory"])
async def test_successful_independent_intents_reach_review_and_commit_in_two_calls(tmp_path, extra):
    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

    hotel, train = SimulatedHotelProvider(), SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"hotel": hotel, "train": train})
    proposal = single_trip()
    query_plan = [{"domain": "hotel", "parameters": {"city": "北京", "keywords": "如家"}}]
    if extra == "multiple":
        query_plan.append({"domain": "hotel", "parameters": {"city": "杭州", "keywords": "汉庭"}})
    elif extra == "bound":
        query_plan[0]["parameters"]["city"] = "上海"
        task = proposal["tripplan"]["tasks"][0]
        task["requires_hotel"] = True
        task["conditions"]["nights"] = 1
        task["items"].append({"domain": "hotel", "parameters": {"keywords": "如家"}})
    elif extra == "weather":
        from trip_assistant.providers.public_query import PublicQueryProvider

        runtime.queries.public = PublicQueryProvider(
            weather_fetch=lambda city: {
                "current_condition": [{"temp_C": "20", "weatherDesc": [{"value": "晴"}]}],
            }
        )
        query_plan = [{"domain": "weather", "parameters": {"city": "上海"}}]
    additional = (
        (
            "memory",
            "write_memory",
            {
                "request_kind": "planning",
                "updates": [{"op": "add", "text": "喜欢沿江散步"}],
            },
        )
        if extra == "memory"
        else (
            "lookup",
            "query",
            {
                "request_kind": "planning",
                "query_plan": query_plan,
            },
        )
    )

    def review(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        if extra == "memory":
            assert payload["memory_results"][0]["status"] == "ok"
            assert "喜欢沿江散步" in json.dumps(payload["memory_results"], ensure_ascii=False)
        else:
            views = payload["query_results"][0]["data"]
            assert len(views) == len(query_plan)
            assert all(v.get("items") or v.get("candidates_in_tripplan") for v in views)
            assert bool(views[0]["task_bindings"]) is (extra == "bound")
            assert views[0]["query"]["city"] == (
                "上海" if extra in {"bound", "weather"} else "北京"
            )
        result = select_and_reply(payload)
        if extra == "memory":
            result.content[0]["input"]["final_answer"] = (
                "已记住你喜欢沿江散步。去上海可以乘模拟车次SIM001。"
            )
        elif extra == "lookup":
            result.content[0]["input"]["final_answer"] = (
                "北京如家酒店的模拟候选已整理好。去上海可以乘模拟车次SIM001。"
            )
        elif extra == "weather":
            result.content[0]["input"]["final_answer"] = (
                "上海当前天气晴。去上海可以乘模拟车次SIM001。"
            )
        elif extra == "bound":
            view = payload["tripplan"]["candidates"][1]
            args = result.content[0]["input"]
            args["tripplan"]["tasks"][0]["selection_refs"]["hotel"] = {
                "query_id": view["query_id"],
                "result_revision": view["result_revision"],
                "candidate_id": view["items"][0]["id"],
            }
            args["final_answer"] = "去上海可以乘模拟车次SIM001，住上海如家酒店的模拟候选。"
        return result

    runtime.model.script = [batch(("plan", "write_trip", proposal), additional), review]
    queries = {
        "bound": "查上海如家酒店，规划明天从重庆去上海的单程火车，住一晚",
        "lookup": "查北京如家酒店，规划明天从重庆去上海的单程火车，不住酒店",
        "multiple": "查北京如家和杭州汉庭，规划明天从重庆去上海的单程火车，不住酒店",
        "weather": "查上海当前天气，规划明天从重庆去上海的单程火车，不住酒店",
        "memory": "记住喜欢沿江散步，规划明天从重庆去上海的单程火车，不住酒店",
    }
    result = await runtime.run(queries[extra])
    assert runtime.model.messages[1][0][0]["content"] == TRIP_REVIEW_PROMPT
    assert result["status"] == "completed"
    assert len(runtime.model.messages) == 2
    assert result["trip"]["operation"] == "select"
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert saved["status"] == "completed"
    assert len(saved["tasks"]) == 1 and saved["tasks"][0]["destination"] == "上海"
    assert any(v["domain"] == "hotel" for v in saved["results_by_query"].values()) is (
        extra == "bound"
    )
    assert len(hotel.calls) == (0 if extra in {"memory", "weather"} else len(query_plan))
    assert len(train.calls) == 1
    assert result["finalization_method"] == "main_agent" and result["fact_issues"] == []
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.parametrize(
    "name,status",
    [
        ("query", "error"),
        ("query", "partial"),
        ("write_memory", "partial"),
        ("write_memory", "error"),
    ],
)
def test_failed_independent_intent_reaches_same_summary_stage(tmp_path, name, status):
    from support.workflow import populated_workflow

    from trip_assistant.memory.planning import review_payload

    runtime = NativeRuntimeFixture(tmp_path)
    saved = runtime.trip_service.store.save(populated_workflow(quoted=True), expected_revision=None)
    runtime.sessions.record_calls([{"id": "extra", "name": name, "arguments": {}}], "turn")
    runtime.sessions.record_result(
        "extra", {"call_id": "extra", "name": name, "status": status, "data": []}, "turn"
    )
    payload = review_payload(
        runtime.sessions, {"turn_id": "turn", "original_query": "查询并规划"}, saved["id"]
    )
    projected = payload["query_results" if name == "query" else "memory_results"]
    assert projected[0]["status"] == status
