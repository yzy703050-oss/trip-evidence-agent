import pytest
from support.native import NativeRuntimeFixture

from trip_assistant.storage.actions import ActionStore


@pytest.mark.asyncio
async def test_native_batch_uses_durable_actions_not_checkpoint_execution_dict(tmp_path):
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget

    runtime = NativeRuntimeFixture(tmp_path)
    call = {
        "id": "r",
        "name": "read",
        "arguments": {"request_kind": "lookup", "resource": "trip_summaries"},
    }
    turn = {"turn_id": "t", "session_id": "s", "user_id": "a"}
    result = await execute_batch(
        [call],
        registry=runtime.registry,
        turn=turn,
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
    )
    actions = ActionStore(runtime.sessions).list("t", origin="model")
    assert len(actions) == 1 and actions[0]["state"] == "completed"
    assert ActionStore(runtime.sessions).result(actions[0]) == result[0]
    assert "executions" not in runtime.sessions.read_state().get("active_turn", {})


def test_accepted_unstarted_action_remains_queued_on_recovery(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path)
    call = {
        "id": "r",
        "name": "read",
        "arguments": {"request_kind": "lookup", "resource": "trip_summaries"},
    }
    runtime.sessions.record_call(call, "t")
    runtime.sessions.checkpoint_turn("t", {"query": "查看行程"})
    store = ActionStore(runtime.sessions)
    store.accept_many(
        "t", [{"operation_id": "t:r", "origin": "model", "kind": "read", "payload": call}]
    )
    restored = restore_turn(runtime.sessions, "t", settings=runtime.settings)
    assert restored["can_resume"] and not restored["unknown_calls"]
    assert store.get("t:r")["state"] == "queued"


@pytest.mark.asyncio
async def test_queued_action_executes_before_model_after_restore(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path)
    call = {
        "id": "r",
        "name": "read",
        "arguments": {"request_kind": "lookup", "resource": "trip_summaries"},
    }
    runtime.sessions.record_call(call, "t")
    runtime.sessions.checkpoint_turn("t", {"query": "查看行程"})
    store = ActionStore(runtime.sessions)
    store.accept_many(
        "t", [{"operation_id": "t:r", "origin": "model", "kind": "read", "payload": call}]
    )
    restored = restore_turn(runtime.sessions, "t", settings=runtime.settings)
    runtime.model.script = ["没有保存的行程。"]
    result = await runtime.run("查看行程", context=restored["context"], budget=restored["budget"])
    assert result["status"] == "completed" and len(runtime.model.messages) == 1
    assert store.get("t:r")["state"] == "completed"
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
async def test_saved_selection_reply_recovers_without_another_model_call(tmp_path, monkeypatch):
    import asyncio
    import json

    from test_batched_intents import select_and_reply, single_trip
    from test_lookup_planning import batch

    from evals.simulated_travel import SimulatedTrainProvider
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    runtime.model.script = [
        batch(("p", "write_trip", single_trip())),
        lambda messages: select_and_reply(json.loads(messages[-1]["content"])),
    ]
    original = runtime.sessions.append

    def interrupted(event):
        if event.get("final"):
            raise asyncio.CancelledError()
        return original(event)

    with monkeypatch.context() as patch:
        patch.setattr(runtime.sessions, "append", interrupted)
        with pytest.raises(asyncio.CancelledError):
            await runtime.run("重庆去上海单程")
    turn_id = runtime.sessions.read_state()["active_turn"]["turn_id"]
    restored = restore_turn(
        runtime.sessions, turn_id, settings=runtime.settings, trip_service=runtime.trip_service
    )
    before = len(runtime.model.messages)
    result = await runtime.run(
        "重庆去上海单程", context=restored["context"], budget=restored["budget"]
    )
    assert result["status"] == "completed" and len(runtime.model.messages) == before
    assert len(runtime.sessions.read_events(role="assistant", event_type="message")) == 1


def test_internal_job_cannot_be_claimed_twice(tmp_path):
    from trip_assistant.runtime.trip_jobs import record_job

    runtime = NativeRuntimeFixture(tmp_path)
    job = {
        "operation_id": "t:p:queries",
        "parent_call_id": "p",
        "trip_plan_id": "trip",
        "expected_revision": 1,
    }
    turn = {"turn_id": "t"}
    record_job(runtime.sessions, turn, job, "queued")
    assert record_job(runtime.sessions, turn, job, "running") is True
    assert record_job(runtime.sessions, turn, dict(job), "running") is False


def test_durable_final_message_settles_reply_without_regenerating(tmp_path):
    from trip_assistant.runtime.actions import stage_reply
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path)
    runtime.sessions.checkpoint_turn(
        "t", {"query": "你好", "execution_state": {"phase": "reply_ready"}}
    )
    operation = stage_reply(runtime.sessions, "t", "你好。", [])
    runtime.sessions.append(
        {
            "event_id": "final:t",
            "type": "message",
            "turn_id": "t",
            "role": "assistant",
            "scope": "session",
            "content": "你好。",
            "final": True,
        }
    )
    restored = restore_turn(runtime.sessions, "t", settings=runtime.settings)
    assert not restored["can_resume"] and not runtime.model.messages
    assert ActionStore(runtime.sessions).get(operation)["state"] == "completed"


def test_final_message_does_not_complete_a_reply_bound_to_another_version(tmp_path):
    from trip_assistant.runtime.actions import stage_reply
    from trip_assistant.runtime.recovery import restore_turn

    runtime = NativeRuntimeFixture(tmp_path)
    runtime.sessions.checkpoint_turn(
        "t", {"query": "行程", "execution_state": {"phase": "reply_ready"}}
    )
    operation = stage_reply(
        runtime.sessions,
        "t",
        "已保存。",
        [{"name": "write_trip", "trip_plan_id": "trip", "revision": 1}],
    )
    runtime.sessions.append(
        {
            "event_id": "final:t",
            "type": "message",
            "turn_id": "t",
            "role": "assistant",
            "scope": "session",
            "content": "已保存。",
            "final": True,
            "trip_plan_refs": [{"id": "trip", "revision": 2}],
        }
    )
    restore_turn(runtime.sessions, "t", settings=runtime.settings)
    assert ActionStore(runtime.sessions).get(operation)["state"] == "rejected"


@pytest.mark.asyncio
async def test_restored_action_rejected_by_preflight_is_not_left_queued(tmp_path):
    from trip_assistant.runtime.execution import execute_batch
    from trip_assistant.runtime.limits import TurnBudget

    runtime = NativeRuntimeFixture(tmp_path)
    call = {
        "id": "r",
        "name": "read",
        "arguments": {"request_kind": "lookup", "resource": "trip_summaries"},
    }
    runtime.sessions.record_call(call, "t")
    store = ActionStore(runtime.sessions)
    store.accept_many(
        "t", [{"operation_id": "t:r", "origin": "model", "kind": "read", "payload": call}]
    )
    result = await execute_batch(
        [call],
        registry={},
        turn={"turn_id": "t"},
        budget=TurnBudget(runtime.settings),
        sessions=runtime.sessions,
        resuming=True,
    )
    assert result[0]["status"] == "rejected"
    assert store.get("t:r")["state"] == "rejected"
    assert not runtime.sessions.pending_tool_calls()
