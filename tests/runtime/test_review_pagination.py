import asyncio
import json

import pytest
from support.native import NativeRuntimeFixture
from test_batched_intents import select_and_reply, single_trip
from test_lookup_planning import batch

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT


@pytest.mark.asyncio
async def test_existing_trip_rejects_pagination_and_keeps_current_candidates(tmp_path):
    from trip_assistant.runtime.limits import TurnBudget

    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[0, 3, 6, 9, 12, 15])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})
    turn = {
        "operation_id": "seed:write",
        "original_query": "重庆去上海单程",
        "budget": TurnBudget(runtime.settings),
    }
    written = await runtime.trip_service.apply(single_trip()["tripplan"], turn)
    await runtime.trip_queries.run(
        written["trip_plan_id"], written["revision"], {**turn, "operation_id": "seed:query"}
    )

    def select(messages):
        payload = json.loads(messages[-1]["content"])
        assert payload["observations"][-1]["status"] == "rejected"
        assert payload["tripplan"]["candidates"][0]["view_offset"] == 0
        assert not any(
            t["function"]["name"] == "read" for t in runtime.model.messages[-1][1]["tools"]
        )
        return select_and_reply(payload)

    runtime.model.script = [next_page, select]
    result = await runtime.run(
        "看后页车次并保存", context={"review_trip_plan_id": written["trip_plan_id"]}
    )
    assert result["status"] == "partial" and result["trip"]["status"] == "completed"
    assert len(runtime.model.messages) == 2 and len(train.calls) == 1


def next_page(messages, **changes):
    payload = json.loads(messages[-1]["content"])
    trip = payload["tripplan"]
    view = trip["candidates"][0]
    assert messages[0]["content"] == TRIP_REVIEW_PROMPT and view["has_more"]
    return batch(
        (
            "page",
            "read",
            {
                "request_kind": "planning",
                "resource": "candidates",
                "trip_plan_id": trip["trip_plan_id"],
                "query_id": view["query_id"],
                "result_revision": view["result_revision"],
                "offset": 5,
                **changes,
            },
        )
    )


@pytest.mark.asyncio
async def test_review_rejects_paging_then_selects_current_page_without_supplier_request(tmp_path):
    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[0, 3, 6, 9, 12, 15])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})

    def select(messages):
        payload = json.loads(messages[-1]["content"])
        view = payload["tripplan"]["candidates"][0]
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        assert view["view_offset"] == 0 and view["items"][0]["train_number"] == "SIM001"
        assert payload["observations"][-1]["status"] == "rejected"
        assert payload["adjustment_history"][0]["operation"] == "propose"
        chosen = select_and_reply(payload)
        chosen.content[0]["input"]["final_answer"] = "已选择当前展示的模拟列车方案。"
        return chosen

    runtime.model.script = [batch(("p", "write_trip", single_trip())), next_page, select]
    result = await runtime.run("规划明天重庆去上海单程旅游，看看后页车次")
    assert result["status"] == "partial" and result["trip"]["operation"] == "select"
    assert result["trip"]["status"] == "completed"
    assert len(runtime.model.messages) == 3 and len(train.calls) == 1
    assert result["fact_issues"] == [] and not runtime.sessions.pending_tool_calls()
    assert [t["function"]["name"] for t in runtime.model.messages[-1][1]["tools"]] == [
        "write_trip",
        "request_replan",
    ]
    assert (
        result["trip"]["validation"]["reconstructed_tasks"][0]["train"]["train_number"] == "SIM001"
    )


@pytest.mark.asyncio
async def test_review_page_is_recovered_from_paired_events_after_interruption(tmp_path):
    from trip_assistant.memory.planning import review_payload
    from trip_assistant.storage.sessions import SessionStore

    train = SimulatedTrainProvider(duration_minutes=120, departure_hours=[0, 3, 6, 9, 12, 15])
    runtime = NativeRuntimeFixture(tmp_path, {"train": train})

    def interrupt(messages):
        raise asyncio.CancelledError()

    runtime.model.script = [batch(("p", "write_trip", single_trip())), next_page, interrupt]
    with pytest.raises(asyncio.CancelledError):
        await runtime.run("规划明天重庆去上海单程旅游")
    result = runtime.sessions.read_events(event_type="tool_result")[0]["content"]
    # Import a genuine old cached-page receipt through the internal handler. New
    # model calls cannot request it; historical event recovery remains supported.
    saved = runtime.trip_service.store.load(result["trip_plan_id"])
    view = saved["last_candidate_views"][0]
    args = {
        "resource": "candidates",
        "trip_plan_id": saved["id"],
        "query_id": view["query_id"],
        "result_revision": view["result_revision"],
        "offset": 5,
    }
    legacy_turn = {
        "candidate_views": {f"{view['query_id']}:{view['result_revision']}": view},
        "shown_candidates": {},
    }
    receipt = await runtime.registry["read"].handler(args, legacy_turn)
    tid = runtime.sessions.read_state()["active_turn"]["turn_id"]
    runtime.sessions.record_calls([{"id": "legacy-page", "name": "read", "arguments": args}], tid)
    runtime.sessions.record_result("legacy-page", {"name": "read", **receipt}, tid)
    restored = SessionStore(tmp_path, "a", "s")
    turn = {
        "turn_id": restored.read_state()["active_turn"]["turn_id"],
        "original_query": "继续规划",
    }
    payload = review_payload(restored, turn, result["trip_plan_id"])
    assert payload["tripplan"]["candidates"][0]["view_offset"] == 5
    assert payload["tripplan"]["candidates"][0]["items"][0]["train_number"] == "SIM006"
    assert len(train.calls) == 1
    changed = runtime.trip_service.store.load(result["trip_plan_id"])
    changed["tasks"][0]["revision"] += 1
    runtime.trip_service.store.save(changed, expected_revision=changed["revision"])
    stale = review_payload(restored, turn, result["trip_plan_id"])
    assert all(view.get("view_offset") != 5 for view in stale["tripplan"]["candidates"])
