import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.native import NativeRuntimeFixture, write_then_query
from test_activities import activity, context

from evals.simulated_travel import SimulatedHotelProvider, SimulatedTrainProvider


@pytest.mark.parametrize("quote_required", [False, True])
def test_hotel_place_recommendation_does_not_prove_quote_or_booking(quote_required):
    from support.workflow import populated_workflow

    from trip_assistant.trips.validation import check_workflow

    work = populated_workflow()
    if quote_required:
        work["confirmed_conditions"]["hotel_quote_required"] = True
    checked = check_workflow(work)
    assert checked["valid"] is not quote_required
    unknown = [i for i in checked["issues"] if i["code"] == "hotel_price_unknown"]
    assert unknown and all(i["blocking"] is quote_required for i in unknown)
    hotel = checked["reconstructed_tasks"][0]["hotel"]
    assert hotel["kind"] == "hotel_place" and hotel.get("stay_total_cny") is None
    assert hotel.get("availability") != "available"
    assert not hotel.get("booking_id")


def batch(*calls):
    return SimpleNamespace(
        content=[
            {"type": "tool_use", "id": identity, "name": name, "input": args}
            for identity, name, args in calls
        ]
    )


def round_trip():
    return {
        "operation": "propose",
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "visit",
                "requires_hotel": True,
                "conditions": {"departure_date": "2026-10-19", "nights": 2},
                "items": [{"domain": "train"}, {"domain": "hotel"}],
                "content": [
                    activity(date="2026-10-20", time="下午"),
                    activity(date="2026-10-21", time="08:00", description="08:00游览，午后返程"),
                ],
            },
            {
                "origin": "上海",
                "destination": "重庆",
                "purpose": "return",
                "conditions": {"departure_date": "2026-10-21"},
                "items": [{"domain": "train"}],
            },
        ],
    }


def choose(trip, *, amend=False):
    rows = []
    for task in trip["tasks"]:
        refs = {}
        for view in trip["candidates"]:
            if view["task_id"] == task["id"]:
                refs[view["domain"]] = {
                    "query_id": view["query_id"],
                    "result_revision": view["result_revision"],
                    "candidate_id": view["items"][0]["id"],
                }
        row = {"task_id": task["id"], "selection_refs": refs}
        if amend and task["purpose"] == "visit":
            row.update(content=task["content"][:1], components=["content", "train", "hotel"])
        rows.append(row)
    return {
        "operation": "amend" if amend else "select",
        "trip_plan_id": trip["trip_plan_id"],
        "expected_revision": trip["revision"],
        "tasks": rows,
    }


@pytest.mark.asyncio
async def test_early_return_select_preserves_committed_until_content_amended(tmp_path):
    train, hotel = SimulatedTrainProvider(departure_hours=[7]), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})
    turn = context(runtime)
    proposed = await write_then_query(runtime.trip_service, round_trip(), turn)
    turn["operation_id"] = "t:select"
    blocked = await write_then_query(runtime.trip_service, choose(proposed), turn)
    saved = runtime.trip_service.store.load(blocked["trip_plan_id"])
    assert blocked["status"] == "partial" and not blocked["validation"]["valid"]
    assert not saved.get("committed")
    problem = next(i for i in blocked["validation"]["issues"] if i["code"].startswith("activity_"))
    assert problem["task_ids"] == [t["id"] for t in blocked["tasks"]]
    calls = len(train.calls), len(hotel.calls)
    turn["operation_id"] = "t:amend"
    completed = await write_then_query(runtime.trip_service, choose(blocked, amend=True), turn)
    assert completed["status"] == "completed"
    saved = runtime.trip_service.store.load(completed["trip_plan_id"])
    committed = deepcopy(saved["committed"])
    assert len(committed["tasks"][0]["content"]) == 1
    assert (
        committed["tasks"][0]["content"]
        == completed["validation"]["reconstructed_tasks"][0]["content"]
    )
    assert (len(train.calls), len(hotel.calls)) == calls
    assert committed["tasks"][0]["content"][0]["id"] == proposed["tasks"][0]["content"][0]["id"]
    turn["operation_id"] = "t:bad-amend"
    bad = {
        "operation": "amend",
        "trip_plan_id": saved["id"],
        "expected_revision": saved["revision"],
        "tasks": [
            {
                "task_id": saved["tasks"][0]["id"],
                "components": ["content"],
                "content": [activity(date="2026-10-21", time="08:00")],
            }
        ],
    }
    failed = await write_then_query(runtime.trip_service, bad, turn)
    assert not failed["validation"]["valid"]
    assert runtime.trip_service.store.load(saved["id"])["committed"] == committed


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_first,interrupt", [(True, False), (False, False), (False, True)])
async def test_activity_amend_reply_and_restore_follow_actual_commit(
    tmp_path, monkeypatch, blocked_first, interrupt
):
    from trip_assistant.runtime.recovery import restore_turn

    train, hotel = SimulatedTrainProvider(departure_hours=[7]), SimulatedHotelProvider()
    runtime = NativeRuntimeFixture(tmp_path, {"train": train, "hotel": hotel})

    def decision(messages, amend):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return batch(
            (
                "amend" if amend else "select",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": choose(trip, amend=amend),
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "final_answer": "已保存调整后的活动。" if amend else "不应放行的取消活动答复。",
                },
            )
        )

    runtime.model.script = [
        batch(
            (
                "p",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": round_trip(),
                },
            )
        )
    ]
    if blocked_first:
        runtime.model.script.append(lambda messages: decision(messages, False))
    runtime.model.script.append(lambda messages: decision(messages, True))
    query = "安排重庆到上海的往返旅游"
    if interrupt:
        original = runtime.sessions.append

        def stop_before_reply(event):
            if event.get("final"):
                raise asyncio.CancelledError()
            return original(event)

        with monkeypatch.context() as patch:
            patch.setattr(runtime.sessions, "append", stop_before_reply)
            with pytest.raises(asyncio.CancelledError):
                await runtime.run(query)
        turn_id = runtime.sessions.read_state()["active_turn"]["turn_id"]
        restored = restore_turn(
            runtime.sessions, turn_id, settings=runtime.settings, trip_service=runtime.trip_service
        )
        before = len(runtime.model.messages)
        result = await runtime.run(query, context=restored["context"], budget=restored["budget"])
        assert len(runtime.model.messages) == before
    else:
        result = await runtime.run(query)
    assert result["final_answer"] == "已保存调整后的活动。"
    assert len(runtime.model.messages) == (3 if blocked_first else 2)
    summaries = runtime.trip_service.store.list_summaries()
    assert len(summaries) == 1
    saved = runtime.trip_service.store.load(result["trip"]["trip_plan_id"])
    assert len(saved["committed"]["tasks"][0]["content"]) == 1
    assert len(train.calls) == 2 and len(hotel.calls) == 1
    final = runtime.sessions.read_events(role="assistant", event_type="message")
    assert len(final) == 1
