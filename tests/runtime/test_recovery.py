import pytest
from support.native import FakeClock

from trip_assistant.settings import Settings
from trip_assistant.storage.sessions import SessionStore


def test_restored_budget_excludes_offline_time_and_keeps_rounds(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    store = SessionStore(tmp_path, "a", "s")
    store.checkpoint_turn(
        "t",
        {
            "query": "规划",
            "active_seconds": 400,
            "planning_rounds_used": 2,
            "planning_started": True,
            "executions": {},
        },
    )
    clock = FakeClock(10000)
    result = restore_turn(store, "t", settings=Settings(), clock=clock)
    assert result["budget"].rounds == 2 and result["budget"].remaining_seconds() == 200


def test_committed_receipt_repairs_missing_tool_result_without_replaying(tmp_path):
    from support.workflow import populated_workflow

    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.storage.trips import TripStore
    from trip_assistant.trips.service import TripService

    store = SessionStore(tmp_path, "a", "s")
    trips = TripStore(tmp_path, "a")
    settings = Settings()
    call = {"id": "w", "name": "write_trip", "arguments": {"tripplan": {}}}
    store.record_call(call, "t")
    store.checkpoint_turn(
        "t",
        {
            "query": "规划",
            "planning_rounds_used": 2,
            "active_seconds": 400,
            "executions": {"w": {"state": "started", "operation_id": "t:w", "name": "write_trip"}},
        },
    )
    work = populated_workflow(quoted=True)
    work["user_id"] = "a"
    saved = trips.save_working(work, expected_revision=None, operation_id="t:w")
    result = restore_turn(
        store, "t", settings=settings, trip_service=TripService(trips, None, Scheduler(settings))
    )
    assert (
        not store.pending_tool_calls()
        and result["can_resume"]
        and trips.load(saved["id"])["revision"] == saved["revision"]
    )
    assert result["budget"].rounds == 2


def test_unknown_started_query_is_paired_as_unknown_not_replayed(tmp_path):
    from trip_assistant.runtime.recovery import restore_turn

    store = SessionStore(tmp_path, "a", "s")
    store.record_call({"id": "q", "name": "query", "arguments": {}}, "t")
    store.checkpoint_turn(
        "t",
        {
            "query": "查询",
            "executions": {"q": {"state": "started", "operation_id": "t:q", "name": "query"}},
        },
    )
    result = restore_turn(store, "t", settings=Settings())
    assert not result["can_resume"] and not store.pending_tool_calls()
    assert store.read_events(event_type="tool_result")[-1]["content"]["status"] == "unknown"


def test_simple_memory_operation_is_atomic_and_idempotent(tmp_path):
    from trip_assistant.storage.preferences import LongTermMemory

    m = LongTermMemory("a", str(tmp_path))
    args = [{"op": "add", "text": "出差尽量高铁"}]
    first = m.apply_memory_updates(args, operation_id="t:m")
    second = m.apply_memory_updates(args, operation_id="t:m")
    assert first == second and len(m.data["travel_memories"]) == 1


def test_clear_context_hides_old_pending_calls_but_keeps_audit(tmp_path):
    store = SessionStore(tmp_path, "a", "s")
    store.record_call({"id": "q", "name": "query", "arguments": {}}, "t")
    store.checkpoint_turn("t", {"query": "查询", "executions": {"q": {"state": "started"}}})
    store.clear_context()
    assert not store.pending_tool_calls()
    assert not store.read_state().get("active_turn")
    assert store.read_events()


@pytest.mark.asyncio
async def test_unknown_restored_operation_cannot_reenter_model_without_new_turn(tmp_path):
    from support.native import make_application

    app = make_application("a", "s", str(tmp_path))
    app.sessions.record_call({"id": "q", "name": "query", "arguments": {}}, "t")
    app.sessions.checkpoint_turn("t", {"query": "查询", "executions": {"q": {"state": "started"}}})
    restored = app.restore("t")
    with pytest.raises(ValueError, match="new user turn"):
        await app.run("查询", restored=restored)
    assert not app.raw_model.messages
    app.raw_model.script = ["请提供本次查询条件。"]
    result = await app.run("重新查询上海天气")
    assert result["turn_id"] != "t"


def test_written_reply_recovery_uses_same_receipt_without_replaying(tmp_path):
    from support.workflow import populated_workflow

    from trip_assistant.presentation.trip_reply import render_trip_reply
    from trip_assistant.runtime.recovery import restore_turn
    from trip_assistant.runtime.scheduler import Scheduler
    from trip_assistant.storage.trips import TripStore
    from trip_assistant.trips.service import TripService
    from trip_assistant.trips.validation import check_workflow

    sessions = SessionStore(tmp_path, "a", "s")
    trips = TripStore(tmp_path, "a")
    settings = Settings(_env_file=None)
    service = TripService(trips, None, Scheduler(settings))
    work = populated_workflow(quoted=True)
    work.update(user_id="a", last_operation="select")
    work["validation"] = check_workflow(work)
    saved = trips.save_working(work, expected_revision=None, operation_id="t:w")
    reply = {"explanation": "保留原有安排。"}
    args = {"tripplan": {"operation": "select", "trip_plan_id": saved["id"]}, "reply": reply}
    sessions.record_call({"id": "w", "name": "write_trip", "arguments": args}, "t")
    sessions.checkpoint_turn(
        "t",
        {
            "query": "保存",
            "executions": {"w": {"state": "started", "operation_id": "t:w", "name": "write_trip"}},
        },
    )
    before = trips.load(saved["id"])["revision"]
    restored = restore_turn(sessions, "t", settings=settings, trip_service=service)
    result = sessions.read_events(event_type="tool_result")[-1]["content"]
    assert result["rendered_answer"] == render_trip_reply(reply, service._result(saved))
    assert trips.load(saved["id"])["revision"] == before
    assert not sessions.pending_tool_calls() and not restored["unknown_calls"]
