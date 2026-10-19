"""Reproduce the rejected Shanghai proposal using the real service and budget."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture, trip_observation

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider
from trip_assistant.runtime.limits import LimitError, TurnBudget
from trip_assistant.settings import Settings


def proposal():
    return {
        "operation": "propose",
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-17", "passengers": 1},
                "condition_strengths": {"departure_date": "hard"},
                "items": [{"domain": "train"}],
            },
            {
                "origin": "上海",
                "destination": "上海",
                "requires_hotel": True,
                "conditions": {"check_in": "2026-10-17", "check_out": "2026-10-20", "nights": 3},
                "condition_strengths": {"check_in": "soft", "check_out": "soft", "nights": "soft"},
                "items": [{"domain": "hotel"}],
            },
            {
                "origin": "上海",
                "destination": "重庆",
                "purpose": "return",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-20"},
                "condition_strengths": {"departure_date": "soft"},
                "items": [{"domain": "train"}],
            },
        ],
    }


def corrected_proposal():
    plan = proposal()
    arrival, stay, back = plan["tasks"]
    arrival["requires_hotel"] = True
    for key in ("conditions", "condition_strengths"):
        arrival[key].update(stay[key])
    arrival["items"] += stay["items"]
    plan["tasks"] = [arrival, back]
    return plan


def response(identity, plan, *, final_answer=None):
    args = {"request_kind": "planning", "tripplan": deepcopy(plan)}
    if plan.get("operation") in {"select", "amend", "confirm"}:
        args["goal_assessment"] = {"status": "satisfied", "reason": "原需求与本次选择相符。"}
    if final_answer:
        args["final_answer"] = final_answer
    return SimpleNamespace(
        content=[
            {
                "type": "tool_use",
                "id": identity,
                "name": "write_trip",
                "input": args,
            }
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stay_task", "missing_stay", "route_gap"])
async def test_preflight_feedback_identifies_task_and_correction(tmp_path, fault):
    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal() if fault == "stay_task" else corrected_proposal()
    if fault == "missing_stay":
        plan["tasks"][0]["conditions"] = {"departure_date": "2026-10-17"}
        plan["tasks"][0]["condition_strengths"] = {"departure_date": "hard"}
    elif fault == "route_gap":
        plan["tasks"][1]["origin"] = "杭州"
    with pytest.raises(ValueError) as caught:
        runtime.trip_service.preview(plan, {})
    message = str(caught.value)
    assert f"tasks[{0 if fault == 'missing_stay' else 1}]" in message
    if fault == "stay_task":
        assert "hotel" in message and "conditions" in message and "tasks[0]" in message
    elif fault == "missing_stay":
        assert "nights" in message and "check_in" in message
    else:
        assert "杭州" in message and "上海" in message and "origin" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("ignore_stop", [False, True])
async def test_three_invalid_proposals_close_tools_and_do_not_loop(tmp_path, ignore_stop):
    train, hotel = SimulatedTrainProvider(), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(
        tmp_path,
        {"train": train, "hotel": hotel},
        settings=Settings(_env_file=None, planning_rounds=3),
    )
    final = "本轮已达到三轮规划上限，提案结构仍有错误，尚未保存行程。"
    runtime.model.script = [response(str(i), proposal()) for i in range(3)] + [
        response("ignored", proposal()) if ignore_stop else final,
        "不应再请求模型",
    ]
    result = await runtime.run("规划明天重庆去上海的行程")
    assert len(runtime.model.messages) == 4
    assert runtime.model.messages[-1][1] == {"tool_choice": "none"}
    assert result["stop_reason"] == "planning_round_limit_exceeded"
    assert result["budget"]["planning_rounds_used"] == 3
    assert not train.calls and not hotel.calls
    assert runtime.trip_service.store.list_summaries() == []
    assert not runtime.sessions.pending_tool_calls()
    assert "上限" in result["final_answer"]


@pytest.mark.asyncio
async def test_restored_exhausted_budget_only_requests_final_answer(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, settings=Settings(_env_file=None, planning_rounds=3))
    budget = TurnBudget(runtime.settings, snapshot={"planning_rounds_used": 3})
    runtime.model.script = ["规划额度已用尽，当前没有已保存的方案。"]
    result = await runtime.run("继续规划", budget=budget)
    assert len(runtime.model.messages) == 1
    assert runtime.model.messages[0][1] == {"tool_choice": "none"}
    assert result["stop_reason"] == "planning_round_limit_exceeded"


def test_oversized_planning_batches_cannot_bypass_rounds():
    budget = TurnBudget(Settings(_env_file=None, planning_rounds=3))
    for i in range(4):
        with pytest.raises(LimitError) as caught:
            budget.reserve_batch([{}] * 9, [], True)
        assert str(caught.value) == (
            "tool_limit_exceeded" if i < 3 else "planning_round_limit_exceeded"
        )
    assert budget.rounds == 3


@pytest.mark.asyncio
async def test_ordinary_reads_can_continue_after_three_batches(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    runtime.model.script = [
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": str(i),
                    "name": "read",
                    "input": {"request_kind": "lookup", "resource": "trip_summaries"},
                }
            ]
        )
        for i in range(4)
    ] + ["暂无已保存行程。"]
    result = await runtime.run("查一下已有行程")
    assert len(runtime.model.messages) == 5
    assert all("tools" in options for _, options in runtime.model.messages)
    assert result["budget"]["planning_rounds_used"] == 0
    assert result["stop_reason"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [True, False])
async def test_repaired_proposal_can_commit_on_third_round(tmp_path, staged):
    train = SimulatedTrainProvider(duration_minutes=720)
    runtime = NativeRuntimeFixture(
        tmp_path,
        {"train": train, "hotel": SimulatedHotelProvider()},
        settings=Settings(_env_file=None, planning_rounds=3),
    )
    answer = (
        "已保存模拟行程，尚未预订。去程重庆到上海 SIM001 二等座，2026-10-17 08:00出发，"
        "当日20:00到达，200元。全季（上海模拟候选1），10月17日入住，10月20日退房，3晚；"
        "只有酒店地点，房价和空房未知。返程上海到重庆 SIM001 二等座，2026-10-20 08:00出发，"
        "当日20:00到达，200元。交通合计400元，住宿与返程日期为软建议。"
    )

    def select(messages):
        draft = trip_observation(messages)
        tasks = []
        for task in draft["tasks"]:
            refs = {
                view["domain"]: {
                    "query_id": view["query_id"],
                    "result_revision": view["result_revision"],
                    "candidate_id": view["items"][0]["id"],
                }
                for view in draft["candidates"]
                if view["query_id"].startswith(task["id"] + ":")
            }
            tasks.append({"task_id": task["id"], "selection_refs": refs})
        return response(
            "select",
            {
                "operation": "select",
                "trip_plan_id": draft["trip_plan_id"],
                "expected_revision": draft["revision"],
                "tasks": tasks,
            },
            final_answer=answer if staged else None,
        )

    runtime.model.script = [
        response("bad", proposal()),
        response("fixed", corrected_proposal()),
        select,
    ] + ([] if staged else [answer])
    result = await runtime.run("规划2026-10-17重庆去上海的行程，停留三晚可调整，安排返程")
    assert result["status"] == "completed" and result["stop_reason"] is None
    assert len(runtime.model.messages) == (3 if staged else 4)
    assert result["budget"]["planning_rounds_used"] == 3
    if not staged:
        assert runtime.model.messages[-1][1] == {"tool_choice": "none"}
    assert result["final_answer"] == answer and result["fact_issues"] == []
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert saved["committed"] and saved["status"] == "completed"
    assert saved["tasks"][0]["conditions"]["departure_date"] == "2026-10-17"
    assert saved["tasks"][0]["condition_strengths"]["departure_date"] == "hard"


@pytest.mark.asyncio
async def test_fourth_interaction_can_select_and_commit_after_two_rejected_proposals(tmp_path):
    from support.native import sourced_trip_reply

    train, hotel = SimulatedTrainProvider(duration_minutes=720), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})

    def select(messages):
        trip = trip_observation(messages)
        tasks = []
        for task in trip["tasks"]:
            refs = {
                view["domain"]: {
                    "query_id": view["query_id"],
                    "result_revision": view["result_revision"],
                    "candidate_id": view["items"][0]["id"],
                }
                for view in trip["candidates"]
                if view["query_id"].startswith(task["id"] + ":")
            }
            tasks.append({"task_id": task["id"], "selection_refs": refs})
        return response(
            "select",
            {
                "operation": "select",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": tasks,
            },
        )

    runtime.model.script = [
        response("bad-1", proposal()),
        response("bad-2", proposal()),
        response("corrected", corrected_proposal()),
        select,
        sourced_trip_reply,
    ]
    result = await runtime.run("规划2026-10-17重庆去上海，住三晚后返程")
    assert result["status"] == "completed" and result["budget"]["planning_rounds_used"] == 4
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert saved["committed"]["revision"] == saved["revision"]
    assert saved["tasks"][0]["conditions"]["departure_date"] == "2026-10-17"
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    assert not runtime.sessions.pending_tool_calls()
