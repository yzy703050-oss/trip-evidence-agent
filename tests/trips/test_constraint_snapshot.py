from copy import deepcopy

import pytest
from support.native import NativeRuntimeFixture


def proposal():
    return {
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "purpose": "business",
                "conditions": {"departure_date": "2026-10-18"},
                "field_sources": {"departure_date": "proposal"},
                "condition_strengths": {"departure_date": "soft"},
                "items": [{"domain": "train"}],
                "content": [
                    {
                        "date": "2026-10-19",
                        "time": "上午",
                        "purpose": "会议",
                        "description": "参加用户要求的会议",
                        "source": "user",
                        "strength": "hard",
                    }
                ],
            }
        ],
    }


async def create(runtime):
    result = await runtime.trip_service.apply(proposal(), {"operation_id": "create"})
    return runtime.memory.workflow_store.load(result["trip_plan_id"])


def amendment(saved, **changes):
    return {
        "operation": "amend",
        "trip_plan_id": saved["id"],
        "expected_revision": saved["revision"],
        "tasks": [{"task_id": saved["tasks"][0]["id"], **changes}],
    }


async def test_one_proposal_establishes_hard_snapshot_without_hardening_soft_travel(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    saved = await create(runtime)
    snapshot = saved["constraint_snapshot"]
    task = snapshot["tasks"][saved["tasks"][0]["id"]]
    assert snapshot["revision"] == 1
    assert "departure_date" not in task["conditions"]
    assert task["activities"][0]["date"] == "2026-10-19"
    assert task["activities"][0]["time"] == "上午"
    assert not runtime.model.messages


async def test_soft_repair_keeps_snapshot_revision_and_main_hard_update_versions_it(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    saved = await create(runtime)
    await runtime.trip_service.apply(
        amendment(saved, conditions={"departure_date": "2026-10-17"}),
        {"operation_id": "soft", "decision_role": "verifier"},
    )
    updated = runtime.memory.workflow_store.load(saved["id"])
    assert updated["constraint_snapshot"] == saved["constraint_snapshot"]
    content = deepcopy(updated["tasks"][0]["content"])
    content[0]["date"] = "2026-10-20"
    await runtime.trip_service.apply(
        amendment(updated, content=content), {"operation_id": "user-change"}
    )
    final = runtime.memory.workflow_store.load(saved["id"])
    assert final["constraint_snapshot"]["revision"] == 2
    assert final["tasks"][0]["content"][0]["date"] == "2026-10-20"


@pytest.mark.parametrize("change", ["delete", "time", "route", "hard_date"])
async def test_verifier_cannot_rewrite_fixed_requirements_or_route(tmp_path, change):
    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal()
    plan["tasks"][0]["conditions"]["arrival_before"] = "2026-10-19T12:00:00+08:00"
    plan["tasks"][0]["condition_strengths"]["arrival_before"] = "hard"
    plan["tasks"][0]["field_sources"]["arrival_before"] = "user"
    result = await runtime.trip_service.apply(plan, {"operation_id": "create"})
    saved = runtime.memory.workflow_store.load(result["trip_plan_id"])
    content = deepcopy(saved["tasks"][0]["content"])
    content[0]["time"] = "下午"
    changes = {
        "delete": {"content": []},
        "time": {"content": content},
        "route": {"destination": "北京"},
        "hard_date": {"conditions": {"arrival_before": "2026-10-20T12:00:00+08:00"}},
    }
    with pytest.raises(ToolFailure) as caught:
        await runtime.trip_service.apply(
            amendment(saved, **changes[change]),
            {"operation_id": "bad", "decision_role": "verifier"},
        )
    assert caught.value.code == "REPLAN_REQUIRED"
    assert runtime.memory.workflow_store.load(saved["id"]) == saved


def test_legacy_snapshot_compiles_without_mutation_or_softening():
    from trip_assistant.trips.constraints import compile_constraints

    work = {
        "confirmed_conditions": {"end_date": "2026-10-22"},
        "tasks": [
            {
                "id": "task",
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-19"},
                "field_sources": {"departure_date": "user"},
                "content": [],
            }
        ],
    }
    before = deepcopy(work)
    snapshot = compile_constraints(work)
    assert snapshot["global"]["conditions"]["end_date"] == "2026-10-22"
    assert snapshot["tasks"]["task"]["conditions"]["departure_date"] == "2026-10-19"
    assert work == before


async def test_task_policy_filters_are_hard_and_verifier_cannot_change_purpose(tmp_path):
    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal()
    plan["tasks"][0]["conditions"]["constraints"] = {"seat_class": "二等座"}
    result = await runtime.trip_service.apply(plan, {"operation_id": "create"})
    saved = runtime.memory.workflow_store.load(result["trip_plan_id"])
    task_id = saved["tasks"][0]["id"]
    assert saved["constraint_snapshot"]["tasks"][task_id]["conditions"]["constraints"] == {
        "seat_class": "二等座"
    }
    with pytest.raises(ToolFailure, match="局部评估"):
        await runtime.trip_service.apply(
            amendment(saved, purpose="visit"),
            {"operation_id": "bad-purpose", "decision_role": "verifier"},
        )


def test_global_date_scope_error_names_field_and_task_without_mutation(tmp_path):
    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal()
    plan["confirmed_conditions"] = {"departure_date": "2026-10-18"}
    plan["tasks"][0]["condition_strengths"]["departure_date"] = "hard"
    plan["tasks"].append(
        {
            "origin": "上海",
            "destination": "重庆",
            "purpose": "return",
            "conditions": {"departure_date": "2026-10-20"},
            "items": [{"domain": "train"}],
        }
    )
    with pytest.raises(ToolFailure) as caught:
        runtime.trip_service.preview(plan, {})
    assert caught.value.code == "CONDITION_SCOPE_CONFLICT"
    assert caught.value.details["field"] == "departure_date"
    assert caught.value.details["task_value"] == "2026-10-20"
    assert "start_date" in caught.value.details["repair_hint"]
    assert not runtime.memory.workflow_store.list_all()


@pytest.mark.parametrize("source", ["user", "proposal"])
def test_same_date_inheritance_feedback_exposes_strength_and_source_without_writes(
    tmp_path, source
):
    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal()
    plan["confirmed_conditions"] = {"start_date": "2026-10-17"}
    plan["field_sources"] = {"start_date": source}
    plan["condition_strengths"] = {"start_date": "hard"}
    plan["tasks"][0]["conditions"]["departure_date"] = "2026-10-17"
    plan["tasks"][0]["field_sources"] = {"departure_date": "proposal"}
    before = deepcopy(plan)
    with pytest.raises(ToolFailure) as caught:
        runtime.trip_service.preview(plan, {})
    error = caught.value
    assert error.code == "CONDITION_SCOPE_CONFLICT"
    assert error.details["global_value"] == error.details["task_value"] == "2026-10-17"
    assert error.details["global_strength"] == "hard"
    assert error.details["task_strength"] == "soft"
    assert error.details["global_source"] == source
    assert error.details["task_source"] == "proposal"
    assert error.details["inherited_from"] == "confirmed_conditions.start_date"
    assert plan == before
    assert not runtime.memory.workflow_store.list_all()

    # User-required dates must inherit hard strength; proposal dates instead belong
    # on the task. Neither repair changes the independently fixed meeting.
    if source == "user":
        plan["tasks"][0]["condition_strengths"]["departure_date"] = "hard"
    else:
        plan.pop("confirmed_conditions")
        plan.pop("field_sources")
        plan.pop("condition_strengths")
    runtime.trip_service.preview(plan, {})
    assert plan["tasks"][0]["content"] == before["tasks"][0]["content"]


def test_item_softening_feedback_identifies_inherited_field_and_preserves_hard_value(tmp_path):
    from trip_assistant.runtime.errors import ToolFailure

    runtime = NativeRuntimeFixture(tmp_path)
    plan = proposal()
    task = plan["tasks"][0]
    task["conditions"]["constraints"] = {"train_kind": "high_speed"}
    task["items"][0].update(
        constraints={"train_kind": "high_speed"}, condition_strengths={"train_kind": "soft"},
    )
    before = deepcopy(plan)
    with pytest.raises(ToolFailure) as caught:
        runtime.trip_service.preview(plan, {})
    error = caught.value
    assert error.code == "QUERY_CONDITION_CONFLICT"
    assert error.details["field"] == "train_kind"
    assert error.details["domain"] == "train"
    assert error.details["inherited_strength"] == "hard"
    assert error.details["item_strength"] == "soft"
    assert error.details["inherited_value"] == "high_speed"
    assert plan == before
    assert not runtime.memory.workflow_store.list_all()
    task["items"][0]["condition_strengths"]["train_kind"] = "hard"
    runtime.trip_service.preview(plan, {})
