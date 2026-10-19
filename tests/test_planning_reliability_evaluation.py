from copy import deepcopy
from datetime import date

import pytest


def test_new_records_use_two_day_activity_anchor_and_old_records_still_work():
    from evals.manual.planning_reliability import CASES, assess

    assert "后天" in CASES["business_policy_ok"]
    row = recorded("business_policy_ok")
    row["activity_date"] = "2026-10-20"
    first = row["trips"][0]["validation"]["reconstructed_tasks"][0]
    first["content"][0]["date"] = "2026-10-20"
    first["schedule"]["check_out"] = "2026-10-21"
    row["trips"][0]["validation"]["reconstructed_tasks"][1]["train"]["departure_at"] = (
        "2026-10-21T07:20:00+08:00"
    )
    assert assess(row)["outcome"] == "complete"
    assert assess(recorded("business_policy_ok"))["outcome"] == "complete"


def test_multicity_assessment_rejects_missing_intermediate_leg():
    from evals.manual.planning_reliability import assess

    row = recorded("multi_city")
    row["trips"][0]["tasks"] = [
        {"origin": "重庆", "destination": "上海"},
        {"origin": "杭州", "destination": "重庆"},
    ]
    assert "missing_required_leg" in assess(row)["failures"]


def test_return_extension_assessment_rejects_unchanged_return_date():
    from evals.manual.planning_reliability import assess

    row = recorded("return_extension")
    row["seed"] = {"tasks": [{}, {"conditions": {"departure_date": "2026-10-20"}}]}
    row["trips"][0]["tasks"] = [{}, {"conditions": {"departure_date": "2026-10-20"}}]
    assert "return_not_extended" in assess(row)["failures"]


async def test_content_seed_remains_future_even_for_a_late_night_request(tmp_path):
    from datetime import datetime

    from support.native import ScriptedModel

    from evals.manual.planning_reliability import FixtureTrain, seed_content
    from evals.simulated_travel import SimulatedHotelProvider
    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    app = create_application(
        Settings(_env_file=None),
        model=ScriptedModel([]),
        storage_root=tmp_path,
        providers={"train": FixtureTrain(), "hotel": SimulatedHotelProvider()},
    )
    work = await seed_content(app, datetime.fromisoformat("2026-10-19T23:59:00+08:00"))
    assert work["tasks"][0]["conditions"]["departure_date"] == "2026-10-20"
    assert work["tasks"][0]["content"][0]["date"] == "2026-10-21"


def recorded(case="flexible_tourism"):
    plans = [
        {
            "purpose": "business" if case.startswith("business") else "visit",
            "train": {"departure_at": "2026-10-18T07:20:00+08:00"},
            "schedule": {
                "arrival_at": "2026-10-18T18:50:00+08:00",
                "check_in": "2026-10-18",
                "check_out": "2026-10-20",
            },
            "hotel": {"hotel_name": "模拟酒店"},
            "content": [
                {"date": "2026-10-19", "time": "上午", "purpose": "会议", "strength": "hard"}
            ],
        },
        {
            "purpose": "return",
            "train": {"departure_at": "2026-10-20T07:20:00+08:00"},
            "schedule": {},
            "content": [],
        },
    ]
    validation = {"valid": True, "issues": [], "reconstructed_tasks": plans}
    return {
        "case": case,
        "fixed_current_time": "2026-10-18T05:00:00+08:00",
        "result": {"status": "completed", "final_answer": "已保存模拟行程。"},
        "trips": [{"status": "completed", "validation": validation, "committed": {"revision": 1}}],
    }


def test_previous_evening_is_before_next_morning_and_needs_that_night_hotel():
    from evals.manual.planning_reliability import assess

    row = recorded()
    assert assess(row)["outcome"] == "complete"
    row["trips"][0]["validation"]["reconstructed_tasks"][0]["schedule"]["check_in"] = "2026-10-19"
    assert "arrival_night_uncovered" in assess(row)["failures"]


def test_past_departure_cannot_count_as_successful_future_planning():
    from evals.manual.planning_reliability import assess

    row = recorded()
    row["trips"][0]["validation"]["reconstructed_tasks"][0]["train"]["departure_at"] = (
        "2026-10-17T07:20:00+08:00"
    )
    assert "selected_departure_in_past" in assess(row)["failures"]


def test_early_return_cannot_pass_even_when_old_validation_says_valid():
    from evals.manual.planning_reliability import assess

    row = recorded("business_policy_ok")
    row["trips"][0]["validation"]["reconstructed_tasks"][1]["train"]["departure_at"] = (
        "2026-10-19T08:00:00+08:00"
    )
    assert "return_before_morning_activity_ends" in assess(row)["failures"]


def test_business_return_next_day_requires_covering_the_previous_night():
    from evals.manual.planning_reliability import assess

    row = recorded("business_policy_ok")
    plans = row["trips"][0]["validation"]["reconstructed_tasks"]
    plans[0]["schedule"]["check_out"] = "2026-10-19"
    assert "overnight_stay_uncovered" in assess(row)["failures"]


def test_tourism_cannot_pass_with_only_transport_and_hotel():
    from evals.manual.planning_reliability import assess

    row = recorded("mixed_early_return")
    row["trips"][0]["validation"]["reconstructed_tasks"][0]["content"] = []
    assert "missing_tourism_activity" in assess(row)["failures"]


@pytest.mark.parametrize("missing", ["committed", "answer"])
def test_claimed_completion_requires_actual_commit_and_answer(missing):
    from evals.manual.planning_reliability import assess

    row = recorded()
    if missing == "committed":
        row["trips"][0]["committed"] = None
    else:
        row["result"]["final_answer"] = ""
    assert assess(row)["outcome"] == "failed"


def test_policy_partial_requires_selected_time_consistent_draft():
    from evals.manual.planning_reliability import assess

    row = recorded("business_policy_missing")
    work = row["trips"][0]
    work.update(status="running", committed=None)
    work["validation"].update(valid=False, issues=[{"code": "policy_unverified", "blocking": True}])
    row["result"]["status"] = "partial"
    assert assess(row)["outcome"] == "expected_partial"
    invalid = deepcopy(row)
    invalid["trips"][0]["validation"]["issues"].append(
        {"code": "activity_window_unverified", "blocking": True}
    )
    assert assess(invalid)["outcome"] == "failed"


@pytest.mark.asyncio
async def test_high_speed_fixture_has_coherent_duration_and_separate_early_return_stress():
    from evals.manual.planning_reliability import FixtureTrain
    from trip_assistant.providers.contracts import TrainQuery

    train = FixtureTrain(early_return=True)
    result = await train.search(TrainQuery("重庆", "上海", date(2026, 10, 18)))
    assert result.items[0]["departure_at"] == "2026-10-18T07:20:00+08:00"
    assert result.items[0]["arrival_at"] == "2026-10-18T14:20:00+08:00"
    from trip_assistant.providers.rail_preferences import train_kind

    assert all(train_kind(item["train_number"]) == "high_speed" for item in result.items)
    normal = await FixtureTrain().search(TrainQuery("上海", "重庆", date(2026, 10, 20)))
    assert any(item["departure_at"][11:16] >= "14:00" for item in normal.items)
    back = await train.search(TrainQuery("上海", "重庆", date(2026, 10, 20)))
    assert all(x["departure_at"][11:16] == "07:20" for x in back.items)


@pytest.mark.asyncio
async def test_policy_fixture_can_verify_actual_selected_workflow():
    from support.workflow import populated_workflow

    from evals.manual.planning_reliability import FixturePolicy
    from trip_assistant.trips.validation import check_workflow

    work = populated_workflow()
    work["policy_context"] = await FixturePolicy().query({})
    checked = check_workflow(work)
    assert not [i for i in checked["issues"] if i["code"].startswith("policy_")]


def test_early_morning_return_with_tourism_activity_is_not_semantic_success():
    from evals.manual.planning_reliability import assess

    row = recorded("mixed_early_return")
    task = row["trips"][0]["validation"]["reconstructed_tasks"][0]
    task["content"] = [{"date": "2026-10-20", "time": "上午", "purpose": "博物馆游览"}]
    assert "early_return_activity_conflict" in assess(row)["failures"]


@pytest.mark.asyncio
async def test_high_speed_simulator_changes_duration_and_identity_but_not_generic_provider():
    from evals.simulated_travel import SimulatedHighSpeedProvider, SimulatedTrainProvider
    from trip_assistant.providers.contracts import TrainQuery
    from trip_assistant.providers.rail_preferences import train_kind

    query = TrainQuery("重庆", "上海", date(2026, 10, 21))
    high_speed = await SimulatedHighSpeedProvider(duration_minutes=420, departure_hours=[8]).search(
        query
    )
    generic = await SimulatedTrainProvider(duration_minutes=690, departure_hours=[8]).search(query)
    assert high_speed.items[0]["arrival_at"] == "2026-10-21T15:00:00+08:00"
    assert train_kind(high_speed.items[0]["train_number"]) == "high_speed"
    assert high_speed.items[0]["time_evidence"]["arrival"] == "simulation_high_speed_duration"
    assert "非真实" in high_speed.message
    assert generic.items[0]["train_number"].startswith("SIM")
    assert generic.items[0]["arrival_at"] == "2026-10-21T19:30:00+08:00"
