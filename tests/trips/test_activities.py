from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture, write_then_query

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.runtime.limits import TurnBudget


def activity(**updates):
    return {
        "date": "2026-10-17",
        "time": "下午",
        "purpose": "游览",
        "description": "沿江散步",
        "source": "proposal",
        "strength": "soft",
        **updates,
    }


def plan(content=None, purpose="visit"):
    task = {
        "origin": "重庆",
        "destination": "上海",
        "purpose": purpose,
        "conditions": {"departure_date": "2026-10-17"},
        "items": [{"domain": "train"}],
    }
    if content is not None:
        task["content"] = content
    return {"operation": "propose", "default_round_trip": False, "tasks": [task]}


def context(runtime, query="规划重庆去上海旅游", **updates):
    return {
        "original_query": query,
        "session_id": "s",
        "operation_id": "t:propose",
        "budget": TurnBudget(runtime.settings),
        "shown_candidates": {},
        "candidate_views": {},
        **updates,
    }


def select_args(result, **task_fields):
    task = result["tasks"][0]
    view = result["candidates"][0]
    return {
        "operation": "select",
        "trip_plan_id": result["trip_plan_id"],
        "expected_revision": result["revision"],
        "tasks": [
            {
                "task_id": task["id"],
                "selection_refs": {
                    "train": {
                        "query_id": view["query_id"],
                        "result_revision": view["result_revision"],
                        "candidate_id": view["items"][0]["id"],
                    }
                },
                **task_fields,
            }
        ],
    }


@pytest.mark.parametrize(
    "day,time,arrival,depart",
    [
        ("2026-10-21", "上午", "2026-10-19T18:50:00+08:00", "2026-10-21T07:20:00+08:00"),
        ("2026-10-19", "晚上", "2026-10-19T18:50:00+08:00", None),
    ],
)
def test_coarse_activity_crossing_travel_boundary_is_unverified(day, time, arrival, depart):
    from trip_assistant.trips.activities import activity_issues

    task = {"id": "out", "content": [activity(id="a", date=day, time=time)]}
    before = deepcopy(task)
    issues = activity_issues(task, {"schedule": {"arrival_at": arrival}}, depart)
    assert len(issues) == 1
    assert issues[0]["code"] == "activity_window_unverified"
    assert issues[0]["blocking"] is False and issues[0]["evidence"] == "a"
    assert "time" in issues[0]["message"]
    assert issues[0]["coarse_window"]["end"] in {"12:00", "24:00"}
    assert issues[0]["travel_window"] == {"arrival_at": arrival, "departure_at": depart}
    assert task == before


@pytest.mark.parametrize(
    "day,time,expected",
    [
        ("2026-10-18", "晚上", "activity_before_arrival"),
        ("2026-10-22", "上午", "activity_after_departure"),
        ("2026-10-19", "上午", "activity_before_arrival"),
        ("2026-10-21", "下午", "activity_after_departure"),
        ("2026-10-20", "10:00", None),
        ("2026-10-20", "上午", None),
        ("2026-10-21", "07:20", "activity_after_departure"),
        ("2026-10-19", "18:50", None),
        ("2026-10-19", "18:49", "activity_before_arrival"),
        ("2026-10-21", "00:00", None),
    ],
)
def test_activity_window_boundaries_preserve_known_facts(day, time, expected):
    from trip_assistant.trips.activities import activity_issues

    issues = activity_issues(
        {"id": "out", "content": [activity(id="a", date=day, time=time)]},
        {"schedule": {"arrival_at": "2026-10-19T18:50:00+08:00"}},
        "2026-10-21T07:20:00+08:00",
    )
    assert [i["code"] for i in issues] == ([expected] if expected else [])


def test_hard_coarse_activity_partial_overlap_is_not_proven_impossible():
    from trip_assistant.trips.activities import activity_issues
    from trip_assistant.trips.service import TripService

    task = {"id": "out", "content": [activity(id="a", time="上午", strength="hard")]}
    original = deepcopy(task)
    issues = activity_issues(task, {"schedule": {"arrival_at": "2026-10-17T09:20:00+08:00"}})
    assert [i["code"] for i in issues] == ["activity_window_unverified"]
    assert not TripService._hard_conflict(None, {"tasks": [task]}, {"issues": issues})
    assert task == original


@pytest.mark.asyncio
async def test_content_persists_select_omission_and_content_only_amend_reuses_evidence(tmp_path):
    provider = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    turn = context(runtime)
    result = await write_then_query(runtime.trip_service, plan([activity()]), turn)
    saved = runtime.trip_service.store.load(result["trip_plan_id"])
    content = saved["tasks"][0]["content"]
    assert content[0]["id"] and content[0]["strength"] == "soft"
    turn["operation_id"] = "t:select"
    chosen = await write_then_query(runtime.trip_service, select_args(result), turn)
    assert chosen["status"] == "completed"
    assert chosen["tasks"][0]["content"] == content
    turn["operation_id"] = "t:amend"
    updated = deepcopy(content)
    updated[0]["description"] = "到博物馆参观"
    amended = await write_then_query(
        runtime.trip_service,
        {
            "operation": "amend",
            "trip_plan_id": result["trip_plan_id"],
            "expected_revision": chosen["revision"],
            "tasks": [
                {"task_id": chosen["tasks"][0]["id"], "components": ["content"], "content": updated}
            ],
        },
        turn,
    )
    assert amended["status"] == "completed" and len(provider.calls) == 1
    assert amended["tasks"][0]["content"][0]["description"] == "到博物馆参观"


@pytest.mark.parametrize("change", ["clear", "soften", "move", "invent_id"])
def test_native_content_edit_keeps_identity_and_strength_validation(change):
    from trip_assistant.trips.activities import apply_content

    original = [
        activity(
            id="activity_a",
            time="上午",
            purpose="会议",
            description="开会",
            source="user",
            strength="hard",
        )
    ]
    updated = deepcopy(original)
    if change == "clear":
        updated = []
    elif change == "soften":
        updated[0]["strength"] = "soft"
    elif change == "move":
        updated[0]["time"] = "下午"
    else:
        updated[0]["id"] = "activity_other"
    if change in {"soften", "invent_id"}:
        with pytest.raises(ValueError):
            apply_content(original, updated)
    else:
        assert apply_content(original, updated) == updated
    assert original[0]["time"] == "上午" and original[0]["strength"] == "hard"


@pytest.mark.asyncio
async def test_business_missing_policy_still_saves_meeting_and_blocks_compliance(tmp_path):
    provider = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    turn = context(
        runtime,
        "明天重庆去上海出差，上午开会",
        policy_context={"status": "no_knowledge", "missing_fields": ["policy"], "constraints": []},
    )
    meeting = activity(
        time="上午", purpose="会议", description="开会", source="user", strength="hard"
    )
    result = await write_then_query(runtime.trip_service, plan([meeting], "business"), turn)
    assert len(provider.calls) == 1 and result["trip_plan_id"]
    turn["operation_id"] = "t:select"
    chosen = await write_then_query(runtime.trip_service, select_args(result), turn)
    assert chosen["status"] == "partial" and chosen["stop_planning"]
    assert "activity_window_unverified" in {i["code"] for i in chosen["validation"]["issues"]}
    assert "policy_unverified" in {i["code"] for i in chosen["validation"]["issues"]}
    assert "?" not in chosen["reason"] and "？" not in chosen["reason"]
    saved = runtime.trip_service.store.load(result["trip_plan_id"])
    assert len(saved["tasks"][0]["content"]) == 1 and saved["status"] != "completed"
    assert not runtime.trip_service.store.list_summaries()[0]["analysis_eligible"]


@pytest.mark.asyncio
async def test_hard_activity_before_arrival_is_not_declared_complete(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=720)}
    )
    turn = context(runtime, "明天去上海，上午开会")
    result = await write_then_query(
        runtime.trip_service,
        plan(
            [
                activity(
                    time="上午", purpose="会议", description="开会", source="user", strength="hard"
                )
            ]
        ),
        turn,
    )
    turn["operation_id"] = "t:select"
    chosen = await write_then_query(runtime.trip_service, select_args(result), turn)
    assert chosen["status"] == "partial"
    assert "activity_before_arrival" in {i["code"] for i in chosen["validation"]["issues"]}


def test_activity_contract_rejects_bad_dates_and_missing_description():
    from trip_assistant.trips.contracts import TripPlanInput

    with pytest.raises(ValueError):
        TripPlanInput.model_validate(plan([activity(date="2026-02-31")]))


def test_new_activity_based_on_preference_is_a_soft_model_proposal():
    from trip_assistant.trips.activities import apply_content

    result = apply_content([], [activity(source="preference")])
    assert result[0]["source"] == "proposal" and result[0]["strength"] == "soft"


def test_model_activity_edit_does_not_require_authorization_keywords():
    from trip_assistant.trips.activities import apply_content

    current = [
        activity(
            id="meeting",
            time="上午",
            purpose="上午会议",
            description="明天上午在上海参加会议",
            source="user",
            strength="hard",
        )
    ]
    updated = deepcopy(current)
    updated[0]["time"] = "下午"
    result = apply_content(current, updated)
    assert result[0]["time"] == "下午" and current[0]["time"] == "上午"
    assert "下午" in result[0]["description"] and "上午" not in result[0]["description"]


def test_native_content_clear_does_not_reinterpret_user_text():
    from trip_assistant.trips.activities import apply_content

    current = [
        activity(
            id=f"meeting_{i}",
            time=period,
            purpose="会议",
            description="开会",
            source="user",
            strength="hard",
        )
        for i, period in enumerate(["上午", "下午"])
    ]
    assert apply_content(current, []) == []
    assert len(current) == 2


@pytest.mark.asyncio
async def test_user_tomorrow_morning_cannot_be_shifted_to_day_after(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn = context(
        runtime, "明天从重庆去上海，明天上午开会", current_time="2026-10-16T12:00:00+08:00"
    )
    wrong = activity(
        date="2026-10-18",
        time="上午",
        purpose="会议",
        description="开会",
        source="user",
        strength="hard",
    )
    with pytest.raises(ValueError, match="activity date"):
        await write_then_query(runtime.trip_service, plan([wrong]), turn)
    assert runtime.trip_service.store.list_summaries() == []


@pytest.mark.asyncio
async def test_explicit_morning_is_preserved_when_model_omits_time(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn = context(runtime, "明天上午开会", current_time="2026-10-16T12:00:00+08:00")
    meeting = activity(purpose="会议", description="开会", source="user", strength="hard")
    meeting.pop("time")
    result = await write_then_query(runtime.trip_service, plan([meeting]), turn)
    assert result["tasks"][0]["content"][0]["time"] == "上午"


@pytest.mark.asyncio
async def test_model_can_advance_soft_departure_while_keeping_tomorrow_activity(tmp_path):
    from evals.simulated_travel import SimulatedHotelProvider

    runtime = NativeRuntimeFixture(
        tmp_path,
        {
            "train": SimulatedTrainProvider(duration_minutes=120, departure_hours=[18]),
            "hotel": SimulatedHotelProvider(),
        },
    )
    turn = context(
        runtime, "安排明天从重庆去上海玩，想上午开始游览", current_time="2026-10-16T12:00:00+08:00"
    )
    proposal = plan([activity(time="上午", source="user", strength="hard")])
    task = proposal["tasks"][0]
    task.update(
        requires_hotel=True,
        condition_strengths={"departure_date": "soft"},
        field_sources={"departure_date": "proposal"},
    )
    task["conditions"].update(
        departure_date="2026-10-16", check_in="2026-10-16", check_out="2026-10-18", nights=2
    )
    task["items"].append({"domain": "hotel"})
    proposed = await write_then_query(runtime.trip_service, proposal, turn)
    chosen = select_args(proposed)
    hotel = next(v for v in proposed["candidates"] if v["domain"] == "hotel")
    chosen["tasks"][0]["selection_refs"]["hotel"] = {
        "query_id": hotel["query_id"],
        "result_revision": hotel["result_revision"],
        "candidate_id": hotel["items"][0]["id"],
    }
    turn["operation_id"] = "t:select"
    result = await write_then_query(runtime.trip_service, chosen, turn)
    assert result["status"] == "completed"
    stored = runtime.trip_service.store.load(result["trip_plan_id"])["committed"]
    assert stored["tasks"][0]["condition_strengths"]["departure_date"] == "soft"
    assert stored["tasks"][0]["content"][0]["date"] == "2026-10-17"
    assert stored["tasks"][0]["content"][0]["strength"] == "hard"
    schedule = result["validation"]["reconstructed_tasks"][0]["schedule"]
    assert schedule["arrival_at"].startswith("2026-10-16T20:00")
    assert schedule["check_in"] == "2026-10-16"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_check_in", [True, False])
async def test_model_nights_proposal_produces_complete_stay_from_actual_arrival(
    tmp_path, has_check_in
):
    from evals.simulated_travel import SimulatedHotelProvider

    runtime = NativeRuntimeFixture(
        tmp_path,
        {"train": SimulatedTrainProvider(duration_minutes=120), "hotel": SimulatedHotelProvider()},
    )
    proposal = plan([])
    task = proposal["tasks"][0]
    task["requires_hotel"] = True
    task["conditions"]["nights"] = 1
    task["field_sources"] = {"nights": "proposal"}
    task["condition_strengths"] = {"nights": "soft"}
    if has_check_in:
        task["conditions"]["check_in"] = "2026-10-17"
    task["items"].append({"domain": "hotel"})
    turn = context(runtime)
    result = await write_then_query(runtime.trip_service, proposal, turn)
    selection = select_args(result)
    hotel = next(v for v in result["candidates"] if v["domain"] == "hotel")
    selection["tasks"][0]["selection_refs"]["hotel"] = {
        "query_id": hotel["query_id"],
        "result_revision": hotel["result_revision"],
        "candidate_id": hotel["items"][0]["id"],
    }
    turn["operation_id"] = "t:s"
    chosen = await write_then_query(runtime.trip_service, selection, turn)
    assert chosen["status"] == "completed"
    schedule = chosen["validation"]["reconstructed_tasks"][0]["schedule"]
    assert schedule["check_in"] == "2026-10-17" and schedule["check_out"] == "2026-10-18"


@pytest.mark.asyncio
async def test_echoing_unchanged_content_does_not_require_content_edit_permission(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn = context(runtime, "明天下午开会")
    result = await write_then_query(
        runtime.trip_service,
        plan(
            [
                activity(
                    time="下午", purpose="会议", description="开会", source="user", strength="hard"
                )
            ]
        ),
        turn,
    )
    turn["operation_id"] = "t:s"
    chosen = await write_then_query(
        runtime.trip_service,
        select_args(result, content=result["tasks"][0]["content"], components=["train"]),
        turn,
    )
    assert chosen["status"] == "completed"


@pytest.mark.asyncio
async def test_native_proposal_must_include_model_planned_return_not_empty_placeholder(tmp_path):
    provider = SimulatedTrainProvider(duration_minutes=120)
    runtime = NativeRuntimeFixture(tmp_path, {"train": provider})
    turn = context(runtime)
    proposal = plan([])
    proposal.pop("default_round_trip")
    with pytest.raises(ValueError, match="return task"):
        await write_then_query(runtime.trip_service, proposal, turn)
    assert not provider.calls and not runtime.trip_service.store.list_summaries()


@pytest.mark.asyncio
async def test_soft_activity_cannot_extend_past_global_hard_end_date(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn = context(runtime)
    proposal = plan([activity(date="2026-10-19")])
    proposal["confirmed_conditions"] = {"end_date": "2026-10-18"}
    proposal["condition_strengths"] = {"end_date": "hard"}
    result = await write_then_query(runtime.trip_service, proposal, turn)
    turn["operation_id"] = "t:s"
    chosen = await write_then_query(runtime.trip_service, select_args(result), turn)
    assert chosen["status"] == "partial"
    assert "activity_outside_trip_window" in {i["code"] for i in chosen["validation"]["issues"]}


@pytest.mark.asyncio
async def test_business_without_any_policy_cannot_commit(tmp_path):
    runtime = NativeRuntimeFixture(
        tmp_path, {"train": SimulatedTrainProvider(duration_minutes=120)}
    )
    turn = context(runtime, "去上海出差")
    result = await write_then_query(runtime.trip_service, plan([], "business"), turn)
    turn["operation_id"] = "t:select"
    chosen = await write_then_query(runtime.trip_service, select_args(result), turn)
    assert chosen["status"] == "partial"
    assert "policy_unverified" in {i["code"] for i in chosen["validation"]["issues"]}


def test_exact_conflict_remains_blocking_while_coarse_overlap_is_advisory():
    from trip_assistant.trips.activities import activity_issues

    plan = {"schedule": {"arrival_at": "2026-10-21T18:50:00+08:00"}}
    task = {"id": "out", "content": [activity(date="2026-10-21", time="18:49")]}
    assert activity_issues(task, plan)[0]["blocking"] is True
    task["content"][0]["time"] = "晚上"
    advisory = activity_issues(task, plan)[0]
    assert advisory["blocking"] is False
    assert advisory["coarse_window"] == {"start": "18:00", "end": "24:00"}
