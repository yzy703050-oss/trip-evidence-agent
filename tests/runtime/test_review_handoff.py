import json

import pytest
from support.native import NativeRuntimeFixture

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.agents.prompts import MAIN_PROMPT, TRIP_REVIEW_PROMPT
from trip_assistant.runtime.execution import execute_batch
from trip_assistant.runtime.limits import TurnBudget


def call(identity, name, **arguments):
    return {"id": identity, "name": name, "arguments": {"request_kind": "planning", **arguments}}


def response(*calls):
    return {
        "content": [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
            for c in calls
        ]
    }


def proposal():
    return {
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-19"},
                "items": [{"domain": "train"}],
            }
        ],
    }


async def test_verifier_handoff_returns_to_main_and_preserves_current_plan(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})

    def handoff(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return response(
            call(
                "handoff",
                "request_replan",
                trip_plan_ids=[trip["trip_plan_id"]],
                reason="需要补充独立资料后比较整体策略",
            )
        )

    def main(messages):
        from trip_assistant.runtime.telemetry import current_model_context

        assert messages[0]["content"] == MAIN_PROMPT
        assert current_model_context()["stage"] == "agent:main"
        payload = json.loads(messages[-1]["content"])
        assert payload["original_query"] == "规划去上海单程"
        assert payload["tripplan"]["tasks"][0]["destination"] == "上海"
        return "现有候选可以使用，暂时保留草稿。"

    runtime.model.script = [response(call("p", "write_trip", tripplan=proposal())), handoff, main]
    result = await runtime.run("规划去上海单程")
    assert result["stop_reason"] is None
    names = {t["function"]["name"] for t in runtime.model.messages[2][1]["tools"]}
    assert {"read", "query", "query_policy", "write_trip"} <= names
    assert len(runtime.model.messages) == 3 and result["budget"]["planning_rounds_used"] == 2
    assert len(runtime.memory.workflow_store.list_all()) == 1


def test_handoff_mode_is_recovered_from_actual_receipts_without_resetting_budget():
    from trip_assistant.runtime.review import decision_role

    results = [
        {"name": "write_trip", "status": "partial", "trip_plan_id": "t"},
        {"name": "request_replan", "status": "ok", "trip_plan_ids": ["t"]},
    ]
    assert decision_role(results) == "main"
    assert decision_role(results + [{"name": "query_policy", "status": "ok"}]) == "main"
    assert decision_role(results + [{"name": "write_trip", "status": "rejected"}]) == "main"
    assert (
        decision_role(results + [{"name": "write_trip", "status": "partial", "trip_plan_id": "t"}])
        == "verifier"
    )


@pytest.mark.parametrize("mixed", [False, True])
async def test_handoff_wrong_target_or_mixed_effect_is_rejected(tmp_path, mixed):
    from trip_assistant.tools.registry import trip_review_registry

    runtime = NativeRuntimeFixture(tmp_path)
    registry = trip_review_registry(runtime.registry, "current")
    calls = [
        call(
            "h",
            "request_replan",
            trip_plan_ids=["current" if mixed else "other"],
            reason="需要重新规划",
        )
    ]
    if mixed:
        calls.append(
            call(
                "w",
                "write_trip",
                tripplan={"operation": "select", "trip_plan_id": "current", "expected_revision": 1},
            )
        )
    result = await execute_batch(
        calls,
        registry=registry,
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )
    assert all(r["status"] == "rejected" for r in result)
    assert result[0]["error"]["code"] == (
        "REPLAN_BATCH_CONFLICT" if mixed else "REPLAN_TARGET_MISMATCH"
    )
    assert not runtime.memory.workflow_store.list_all()


async def test_actual_planning_allowance_stays_in_executor_not_model_prompt(tmp_path):
    from trip_assistant.settings import Settings

    runtime = NativeRuntimeFixture(tmp_path, settings=Settings(_env_file=None, planning_rounds=6))
    runtime.model.script = ["你好"]
    await runtime.run("你好")
    messages = runtime.model.messages[0][0]
    text = json.dumps(messages, ensure_ascii=False)
    assert "首次复杂写起最多3轮" not in text
    assert "执行额度（数据）" not in text
    assert runtime.settings.planning_rounds == 6


async def test_handoff_receipt_survives_crash_without_replaying_queries(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    class Crash(BaseException):
        pass

    provider = SimulatedTrainProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})

    def handoff(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return response(
            call("h", "request_replan", trip_plan_ids=[trip["trip_plan_id"]], reason="补查整体方案")
        )

    def crash(messages):
        raise Crash()

    runtime.model.script = [response(call("p", "write_trip", tripplan=proposal())), handoff, crash]
    with pytest.raises(Crash):
        await runtime.run("规划去上海单程")
    renewed = NativeRuntimeFixture(tmp_path, {"train": provider})
    active = renewed.sessions.read_state()["active_turn"]
    restored = restore_turn(
        renewed.sessions,
        active["turn_id"],
        settings=renewed.settings,
        trip_service=renewed.trip_service,
    )

    def main(messages):
        assert messages[0]["content"] == MAIN_PROMPT
        assert json.loads(messages[-1]["content"])["replan_request"]["reason"] == "补查整体方案"
        return "已有候选保留，尚未完成选择。"

    renewed.model.script = [main]
    result = await renewed.run(
        "规划去上海单程", context=restored["context"], budget=restored["budget"]
    )
    assert result["stop_reason"] is None
    assert result["budget"]["planning_rounds_used"] == 2
    assert len(provider.calls) == 1 and len(renewed.model.messages) == 1


async def test_main_write_after_handoff_returns_to_verifier(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})

    def handoff(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return response(
            call("h", "request_replan", trip_plan_ids=[trip["trip_plan_id"]], reason="重新组织查询")
        )

    def main(messages):
        assert messages[0]["content"] == MAIN_PROMPT
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return response(
            call(
                "m",
                "write_trip",
                tripplan={
                    "operation": "amend",
                    "trip_plan_id": trip["trip_plan_id"],
                    "expected_revision": trip["revision"],
                    "tasks": [{"task_id": trip["tasks"][0]["id"], "items": [{"domain": "train"}]}],
                },
            )
        )

    def verifier(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        return "查询已完成，保留当前候选供比较。"

    runtime.model.script = [
        response(call("p", "write_trip", tripplan=proposal())),
        handoff,
        main,
        verifier,
    ]
    result = await runtime.run("规划去上海单程")
    assert result["stop_reason"] is None and len(runtime.model.messages) == 4
