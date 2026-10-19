from copy import deepcopy

import pytest
from support.native import write_then_query


def review_snapshot(revision=1, candidate_revision=1, description="散步"):
    return {
        "tripplan": {
            "trip_plan_id": "trip",
            "revision": revision,
            "tasks": [
                {"id": "task", "revision": revision, "content": [{"description": description}]}
            ],
            "candidates": [{"query_id": "q", "result_revision": candidate_revision}],
            "validation": {"issues": [{"code": "activity_before_arrival", "blocking": True}]},
        },
        "previous_round_summary": str(revision),
    }


def test_no_progress_ignores_revision_and_summary_and_survives_checkpoint():
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    state.observe_review(review_snapshot())
    state.observe_review(review_snapshot(revision=2))
    restored = ExecutionState(state.checkpoint())
    restored.observe_review(review_snapshot(revision=3))
    assert restored.summary_only and restored.stop_reason == "no_progress"


def test_real_content_or_evidence_change_resets_no_progress():
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    for i in range(1, 5):
        state.observe_review(review_snapshot(revision=i, candidate_revision=i))
        assert not state.summary_only
    state.observe_review(review_snapshot(revision=5, candidate_revision=4, description="博物馆"))
    assert not state.summary_only


@pytest.mark.asyncio
async def test_loop_stops_repeated_selection_without_business_progress(tmp_path):
    import json
    from types import SimpleNamespace

    from support.native import NativeRuntimeFixture

    from evals.simulated_travel import SimulatedTrainProvider

    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    counter = 0

    def call(identity, plan):
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": identity,
                    "name": "write_trip",
                    "input": {"request_kind": "planning", "tripplan": plan},
                }
            ]
        )

    def repeat(messages):
        nonlocal counter
        counter += 1
        trip = json.loads(messages[-1]["content"])["tripplan"]
        view = trip["candidates"][0]
        return call(
            str(counter),
            {
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
        )

    proposed = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-17"},
                "items": [{"domain": "train"}],
                "content": [
                    {
                        "date": "2026-10-17",
                        "time": "07:00",
                        "purpose": "散步",
                        "description": "抵达后散步",
                        "source": "proposal",
                        "strength": "soft",
                    }
                ],
            }
        ],
    }
    runtime.model.script = [
        call("propose", proposed),
        repeat,
        repeat,
        repeat,
        "实际活动仍冲突，未完成。",
    ]
    result = await runtime.run("规划重庆到上海单程及散步")
    assert result["stop_reason"] == "no_progress"
    assert result["budget"]["planning_rounds_used"] < runtime.settings.planning_rounds
    assert len(runtime.model.messages) == 5
    assert any(
        r.get("type") == "review_progress" and r.get("unchanged_reviews") == 2
        for r in runtime.sessions.read_runs()
    )


def expired_result():
    return {
        "name": "write_trip",
        "call_id": "select",
        "status": "rejected",
        "error": {
            "code": "EVIDENCE_EXPIRED",
            "details": {
                "trip_plan_id": "trip",
                "task_id": "task",
                "domain": "train",
                "query_id": "query",
                "result_revision": 1,
            },
        },
    }


def selection(revision=1):
    return {
        "id": "s",
        "name": "write_trip",
        "arguments": {
            "request_kind": "planning",
            "goal_assessment": {
                "status": "satisfied",
                "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
            },
            "tripplan": {
                "operation": "select",
                "trip_plan_id": "trip",
                "expected_revision": 2,
                "tasks": [
                    {
                        "task_id": "task",
                        "selection_refs": {
                            "train": {
                                "query_id": "query",
                                "result_revision": revision,
                                "candidate_id": "c",
                            }
                        },
                    }
                ],
            },
        },
    }


def test_terminal_state_rejects_side_effect_transition():
    from trip_assistant.runtime.errors import ToolFailure
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    state.transition("reply_ready")
    state.transition("completed")
    with pytest.raises(ToolFailure) as caught:
        state.transition("executing")
    assert caught.value.code == "INVALID_STATE_TRANSITION"


def test_expiry_state_is_structured_durable_and_rejects_only_old_version():
    from trip_assistant.runtime.errors import ToolFailure
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    state.transition("executing")
    state.transition("validating")
    state.observe([expired_result()])
    assert state.phase == "repair_required"
    restored = ExecutionState(deepcopy(state.checkpoint()))
    with pytest.raises(ToolFailure) as caught:
        restored.guard([selection()])
    assert caught.value.code == "EVIDENCE_REFRESH_REQUIRED"
    restored.guard([selection(2)])
    amend = selection()
    amend["arguments"]["tripplan"]["tasks"][0].pop("selection_refs")
    amend["arguments"]["tripplan"].update(operation="amend")
    restored.guard([amend])


def test_second_invalid_reference_stops_without_reading_error_message():
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    state.transition("executing")
    state.transition("validating")
    state.observe([expired_result()])
    repeated = expired_result()
    repeated["reason"] = "这段文字不能决定状态"
    repeated["error"]["code"] = "EVIDENCE_REFRESH_REQUIRED"
    state.transition("deciding")
    state.transition("executing")
    state.transition("validating")
    state.observe([repeated])
    assert state.summary_only and state.stop_reason == "no_progress"


def test_reason_prose_does_not_create_expiry_state():
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState()
    state.transition("executing")
    state.transition("validating")
    state.observe([{"name": "query", "status": "error", "reason": "candidate evidence expired"}])
    assert state.checkpoint()["blocked_evidence"] == []


def test_negative_reply_is_preserved_and_save_is_decided_from_json():
    from trip_assistant.presentation.facts import finalize

    answer = "查询结果已过期未能保存，目前只能参考，尚未最终落定。"
    results = [expired_result()]
    checked = finalize(answer, results)
    assert checked["answer"] == answer and checked["method"] == "main_agent"
    assert checked["execution_facts"]["saved"] is False


def test_invalid_success_receipt_is_not_a_saved_fact():
    from trip_assistant.presentation.facts import finalize

    checked = finalize(
        "这里的文字不参与状态判断。",
        [
            {
                "name": "write_trip",
                "status": "completed",
                "revision": 9,
                "trip_plan_id": "trip",
                "validation": {"valid": False},
            }
        ],
    )
    assert checked["execution_facts"]["saved"] is False
    assert "invalid_save_receipt" in checked["issues"]


@pytest.mark.asyncio
async def test_expired_selection_then_duplicate_is_stopped_and_negative_summary_survives(tmp_path):
    import json
    from copy import deepcopy

    from support.native import NativeRuntimeFixture
    from test_batched_intents import select_and_reply, single_trip
    from test_lookup_planning import batch

    from evals.simulated_travel import SimulatedTrainProvider

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    saved_choice = {}
    summary = "查询结果已过期未能保存，当前只能参考，尚未最终落定。"

    def expire(messages):
        runtime.scheduler.settings = runtime.settings.model_copy(
            update={"evidence_max_age_seconds": -1}
        )
        choice = select_and_reply(json.loads(messages[-1]["content"]))
        saved_choice["response"] = deepcopy(choice)
        return choice

    def repeat(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["previous_round_summary"]["results"][0]["selection_saved"] is False
        assert payload["tripplan"]["candidates"][0]["result_revision"] == 2
        choice = deepcopy(saved_choice["response"])
        choice.content[0]["id"] = "repeat"
        return choice

    runtime.model.script = [batch(("p", "write_trip", single_trip())), expire, repeat, summary]
    result = await runtime.run("规划重庆去上海单程，候选过期也不要谎称保存")
    assert result["stop_reason"] == "no_progress" and result["status"] == "partial"
    assert result["final_answer"] == summary and result["finalization_method"] == "main_agent"
    assert result["tool_results"][1]["selection_saved"] is False
    assert result["tool_results"][2]["error"]["code"] == "EVIDENCE_REFRESH_REQUIRED"
    assert len(train.calls) == 2 and len(runtime.model.messages) == 4
    assert runtime.model.messages[-1][1] == {"tool_choice": "none"}
    assert runtime.sessions.read_state()["active_turn"]["execution_state"]["phase"] == "stopped"
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_expired_existing_trip_auto_refreshes_and_selects_in_two_rounds(tmp_path):
    import json

    from support.native import NativeRuntimeFixture
    from test_batched_intents import select_and_reply, single_trip

    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.runtime.limits import TurnBudget

    train = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    seed = await write_then_query(
        runtime.trip_service,
        single_trip()["tripplan"],
        {
            "operation_id": "seed:p",
            "budget": TurnBudget(runtime.settings),
            "original_query": "规划重庆去上海单程",
            "session_id": "s",
        },
    )

    def expire(messages):
        runtime.scheduler.settings = runtime.settings.model_copy(
            update={"evidence_max_age_seconds": -1}
        )
        return select_and_reply(json.loads(messages[-1]["content"]))

    def choose(messages):
        runtime.scheduler.settings = runtime.settings
        payload = json.loads(messages[-1]["content"])
        assert payload["tripplan"]["candidates"][0]["result_revision"] == 2
        response = select_and_reply(payload)
        response.content[0]["id"] = "select-refreshed"
        return response

    runtime.model.script = [expire, choose]
    result = await runtime.run(
        "继续安排这个单程", context={"review_trip_plan_id": seed["trip_plan_id"]}
    )
    assert result["status"] == "completed" and result["stop_reason"] is None
    assert len(train.calls) == 2 and len(runtime.model.messages) == 2
    assert result["budget"]["planning_rounds_used"] == 2
    assert result["execution_facts"]["saved"] is True


def test_restore_rebuilds_expiry_guard_from_paired_result_before_phase_checkpoint(tmp_path):
    from trip_assistant.runtime.errors import ToolFailure
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.state import ExecutionState
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "a", "s")
    store.record_calls([selection()], "t")
    result = expired_result()
    result["call_id"] = "s"
    store.record_result("s", result, "t")
    store.checkpoint_turn(
        "t",
        {
            "status": "running",
            "planning_rounds_used": 1,
            "execution_state": {"phase": "executing"},
        },
    )
    restored = restore_turn(store, "t", settings=Settings(_env_file=None))
    state = ExecutionState(restored["context"]["execution_state"])
    assert restored["can_resume"] and restored["budget"].rounds == 1
    assert state.phase == "repair_required"
    with pytest.raises(ToolFailure):
        state.guard([selection()])


@pytest.mark.asyncio
async def test_invalid_arguments_are_validated_before_repair_guard(tmp_path):
    from support.native import NativeRuntimeFixture

    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.state import ExecutionState

    runtime = NativeRuntimeFixture(tmp_path)
    state = ExecutionState(
        {"phase": "executing", "blocked_evidence": [expired_result()["error"]["details"]]}
    )
    calls = [
        {
            "id": "bad",
            "name": "write_trip",
            "arguments": {"request_kind": "planning", "tripplan": None},
        }
    ]
    results = await execute_batch(
        calls,
        registry=runtime.registry,
        sessions=runtime.sessions,
        budget=TurnBudget(runtime.settings),
        turn={"turn_id": "t", "execution_machine": state},
    )
    assert results[0]["error"]["code"] == "INVALID_ARGUMENTS"
    assert not runtime.sessions.pending_tool_calls()


def test_missing_query_parameters_have_structured_error_code(tmp_path):
    from support.native import NativeRuntimeFixture

    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    with pytest.raises(ToolFailure) as caught:
        runtime.queries.prepare({"domain": "weather", "parameters": {}})
    assert caught.value.code == "MISSING_QUERY_PARAMETERS"
    assert caught.value.details["fields"] == ["city"]


@pytest.mark.asyncio
async def test_repeated_expired_selection_aborts_whole_batch_before_memory_write(tmp_path):
    from support.native import NativeRuntimeFixture

    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget
    from trip_assistant.runtime.state import ExecutionState

    runtime = NativeRuntimeFixture(tmp_path)
    state = ExecutionState(
        {"phase": "executing", "blocked_evidence": [expired_result()["error"]["details"]]}
    )
    before = deepcopy(runtime.memory.data)
    memory = {
        "id": "memory",
        "name": "write_memory",
        "arguments": {
            "request_kind": "planning",
            "updates": [{"op": "add", "text": "喜欢安静酒店"}],
        },
    }
    results = await execute_batch(
        [memory, selection()],
        registry=runtime.registry,
        sessions=runtime.sessions,
        budget=TurnBudget(runtime.settings),
        turn={"turn_id": "t", "execution_machine": state},
    )
    assert results[0]["error"]["code"] == "BATCH_ABORTED"
    assert results[1]["error"]["code"] == "EVIDENCE_REFRESH_REQUIRED"
    assert runtime.memory.data == before and not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_invalid_completed_tool_result_cannot_release_staged_answer(tmp_path):
    from dataclasses import replace

    from support.native import NativeRuntimeFixture
    from test_lookup_planning import batch

    runtime = NativeRuntimeFixture(tmp_path)

    async def bad_receipt(args, turn):
        return {
            "status": "completed",
            "trip_plan_id": "trip",
            "revision": 2,
            "operation": "select",
            "validation": {"valid": False},
        }

    runtime.registry["write_trip"] = replace(
        runtime.registry["write_trip"], handler=bad_receipt, expand_queries=lambda args, turn: []
    )
    call = selection()
    call["arguments"]["final_answer"] = "这段预写答复不得放行。"
    answer = "没有获得有效保存结果，当前安排尚未落定。"
    runtime.model.script = [batch(("s", "write_trip", call["arguments"])), answer]
    result = await runtime.run("继续安排已有行程")
    assert result["final_answer"] == answer and result["status"] == "partial"
    assert result["tool_results"][0]["error"]["code"] == "INVALID_SAVE_RECEIPT"
    assert result["execution_facts"]["saved"] is False
    assert len(runtime.model.messages) == 2


@pytest.mark.parametrize("phase", ["completed", "stopped", "paused"])
def test_restore_terminal_phase_cannot_resume_even_before_legacy_status_checkpoint(tmp_path, phase):
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "a", "s")
    store.checkpoint_turn("t", {"status": "running", "execution_state": {"phase": phase}})
    restored = restore_turn(store, "t", settings=Settings(_env_file=None))
    assert restored["can_resume"] is False


def test_reply_ready_can_record_stop_reason_without_reopening_tools():
    from trip_assistant.runtime.state import ExecutionState

    state = ExecutionState({"phase": "reply_ready"})
    state.stop("planning_round_limit_exceeded")
    assert state.summary_only and state.stop_reason == "planning_round_limit_exceeded"


@pytest.mark.asyncio
async def test_failed_refresh_keeps_committed_plan_and_reports_partial(tmp_path):
    from support.native import NativeRuntimeFixture
    from test_batched_intents import select_and_reply, single_trip
    from test_lookup_planning import batch

    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.runtime.limits import TurnBudget

    class FailingRefresh(SimulatedTrainProvider):
        failed = False

        async def search(self, query):
            if self.failed:
                self.calls.append(query.departure_date)
                raise ValueError("simulated refresh failure")
            return await super().search(query)

    train = FailingRefresh(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = {
        "operation_id": "seed:p",
        "budget": TurnBudget(runtime.settings),
        "original_query": "规划重庆去上海单程",
        "session_id": "s",
    }
    draft = await write_then_query(runtime.trip_service, single_trip()["tripplan"], turn)
    selected = select_and_reply({"tripplan": draft}).content[0]["input"]["tripplan"]
    completed = await write_then_query(
        runtime.trip_service, selected, {**turn, "operation_id": "seed:s"}
    )
    original = runtime.trip_service.store.load(completed["trip_plan_id"])["committed"]
    train.failed = True
    amendment = {
        "request_kind": "planning",
        "goal_assessment": {
            "status": "satisfied",
            "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
        },
        "tripplan": {
            "operation": "amend",
            "trip_plan_id": completed["trip_plan_id"],
            "expected_revision": completed["revision"],
            "tasks": [
                {
                    "task_id": completed["tasks"][0]["id"],
                    "components": ["train"],
                    "items": [{"domain": "train", "refresh": True}],
                }
            ],
        },
    }
    answer = "车次刷新失败，本次修改未能完成，原有效行程仍保留。"
    runtime.model.script = [batch(("refresh", "write_trip", amendment)), answer]
    result = await runtime.run(
        "刷新这次行程的车次", context={"review_trip_plan_id": completed["trip_plan_id"]}
    )
    saved = runtime.trip_service.store.load(completed["trip_plan_id"])
    assert result["status"] == "partial" and result["final_answer"] == answer
    assert saved["committed"] == original
    assert any(row.get("needs_revalidation") for row in saved["results_by_query"].values())
    assert result["budget"]["planning_rounds_used"] == 1
    assert len(runtime.model.messages) == 2 and len(train.calls) == 2


def test_final_message_written_before_terminal_checkpoint_is_not_regenerated(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.settings import Settings
    from trip_assistant.storage.sessions import SessionStore

    store = SessionStore(tmp_path, "a", "s")
    store.append(
        {
            "event_id": "final:t",
            "turn_id": "t",
            "scope": "session",
            "type": "message",
            "role": "assistant",
            "content": "已有结果已整理。",
            "final": True,
        }
    )
    store.checkpoint_turn("t", {"status": "running", "execution_state": {"phase": "reply_ready"}})
    restored = restore_turn(store, "t", settings=Settings(_env_file=None))
    assert restored["can_resume"] is False
    assert restored["context"]["execution_state"]["phase"] == "completed"
    assert len(store.read_events(event_type="message")) == 1
