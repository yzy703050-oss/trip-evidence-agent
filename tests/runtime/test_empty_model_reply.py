from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider


@pytest.mark.asyncio
async def test_empty_body_is_conditionally_regenerated_without_program_authored_normal_reply(
    tmp_path,
):
    runtime = NativeRuntimeFixture(tmp_path)
    runtime.model.script = [SimpleNamespace(content=[]), "这是模型重新生成的答复。"]
    result = await runtime.run("你好")
    assert result["final_answer"] == "这是模型重新生成的答复。"
    assert len(runtime.model.messages) == 2 and not result["tool_results"]
    assert result["budget"]["planning_rounds_used"] == 0


@pytest.mark.asyncio
async def test_empty_hard_conflict_summary_keeps_tools_closed_while_regenerating(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=720, departure_hours=[7]),
        },
    )
    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "conditions": {"departure_date": "2026-10-17"},
                "condition_strengths": {"departure_date": "hard"},
                "content": [
                    {
                        "date": "2026-10-17",
                        "time": "上午",
                        "purpose": "会议",
                        "description": "上海会议",
                        "source": "user",
                        "strength": "hard",
                    }
                ],
                "items": [{"domain": "train"}],
            }
        ],
    }
    runtime.model.script = [
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "p",
                    "name": "write_trip",
                    "input": {"request_kind": "planning", "tripplan": plan},
                }
            ]
        ),
        SimpleNamespace(content=[]),
        "已查车次晚间到达，不能满足当天上午会议，原硬条件保持。",
    ]
    result = await runtime.run(
        "安排17日出发并在上海上午开会",
        context={
            "current_time": "2026-10-17T05:00:00+08:00",
        },
    )
    assert "当天上午会议" in result["final_answer"]
    assert len(runtime.model.messages) == 3 and result["stop_reason"] == "hard_constraint_conflict"
    assert all(options == {"tool_choice": "none"} for _, options in runtime.model.messages[1:])
    assert result["budget"]["planning_rounds_used"] == 1


@pytest.mark.asyncio
async def test_empty_reply_regeneration_is_bounded_and_resume_does_not_reset_it(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    for _ in range(2):
        runtime.sessions.append_run({"type": "empty_model_response", "turn_id": "t"})
    runtime.model.script = [SimpleNamespace(content=[])] * 4
    result = await runtime.run("继续", context={"turn_id": "t"})
    assert len(runtime.model.messages) == 1
    assert result["status"] == "partial" and result["stop_reason"] == "empty_model_response"


def invalid_native_response(messages):
    from trip_assistant.llm.client import ModelProtocolError

    raise ModelProtocolError(
        "native tool arguments are not valid JSON",
        {"prompt_tokens": 9, "completion_tokens": 30},
        finish_reason="tool_calls",
    )


@pytest.mark.asyncio
async def test_invalid_native_generation_is_regenerated_before_any_tools_execute(tmp_path):
    from support.native import trip_observation

    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[7])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "conditions": {"departure_date": "2026-10-17"},
                "items": [{"domain": "train"}],
            }
        ],
    }

    def choose(messages):
        trip = trip_observation(messages)
        view = trip["candidates"][0]
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "s",
                    "name": "write_trip",
                    "input": {
                        "request_kind": "planning",
                        "goal_assessment": {
                            "status": "satisfied",
                            "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                        },
                        "final_answer": "模拟方案已保存。",
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
                    },
                }
            ]
        )

    runtime.model.script = [
        invalid_native_response,
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "p",
                    "name": "write_trip",
                    "input": {"request_kind": "planning", "tripplan": plan},
                }
            ]
        ),
        choose,
    ]
    result = await runtime.run("规划重庆去上海单程火车")
    assert result["status"] == "completed" and result["final_answer"] == "模拟方案已保存。"
    assert len(runtime.model.messages) == 3 and len(train.calls) == 1
    assert result["budget"]["planning_rounds_used"] == 2
    assert len(runtime.trip_service.store.list_summaries()) == 1


@pytest.mark.asyncio
async def test_invalid_native_and_empty_response_share_one_persisted_retry_allowance(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    for _ in range(2):
        runtime.sessions.append_run({"type": "empty_model_response", "turn_id": "t"})
    runtime.model.script = [invalid_native_response] * 4
    result = await runtime.run("继续", context={"turn_id": "t"})
    assert len(runtime.model.messages) == 1 and result["status"] == "partial"
    assert sum(r["type"] == "invalid_model_response" for r in runtime.sessions.read_runs()) == 1
