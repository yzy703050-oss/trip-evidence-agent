"""Product defaults must evaluate supplier facts and allow a local tool repair."""

import json
from copy import deepcopy

import pytest
from support.native import ScriptedModel

from evals.simulated_travel import SimulatedTrainProvider
from trip_assistant.bootstrap import create_application
from trip_assistant.settings import Settings


def response(identity, plan, **extra):
    return {
        "content": [
            {
                "type": "tool_use",
                "id": identity,
                "name": "write_trip",
                "input": {
                    "request_kind": "planning",
                    "tripplan": plan,
                    **extra,
                },
            }
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_default_application_reviews_real_candidates_and_saves_without_extra_call(
    tmp_path, repair
):
    from trip_assistant.agents.prompts import TRIP_REVIEW_PROMPT

    query = "后天重庆到上海，只安排单程火车和抵达后散步。"
    train = SimulatedTrainProvider(duration_minutes=690, departure_hours=[7])
    proposal = {
        "operation": "propose",
        "default_round_trip": False,
        "reason": "先估算铁路占用约10小时，再安排抵达后的软活动。",
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-21"},
                "content": [
                    {
                        "date": "2026-10-21",
                        "time": "下午" if repair else "晚上",
                        "purpose": "散步",
                        "description": "抵达后散步",
                        "source": "proposal",
                        "strength": "soft",
                    }
                ],
                "items": [{"domain": "train"}],
            }
        ],
    }

    def review(messages):
        assert messages[0]["content"] == TRIP_REVIEW_PROMPT
        payload = json.loads(messages[-1]["content"])
        assert payload["original_query"] == query
        trip = payload["tripplan"]
        view = trip["candidates"][0]
        candidate = view["items"][0]
        assert "18:30" in candidate["arrival_at"]
        assert trip["tasks"][0]["content"][0]["time"] == ("下午" if repair else "晚上")
        task = {
            "task_id": trip["tasks"][0]["id"],
            "selection_refs": {
                "train": {
                    "query_id": view["query_id"],
                    "result_revision": view["result_revision"],
                    "candidate_id": candidate["id"],
                }
            },
        }
        if repair:
            content = deepcopy(trip["tasks"][0]["content"])
            content[0]["time"] = "晚上"
            task.update(components=["content", "train"], content=content)
        return response(
            "review",
            {
                "operation": "amend" if repair else "select",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": [task],
            },
            final_answer="10月21日7点出发，18点30分抵达上海，晚上抵达后散步。",
            round_summary="按实际抵达时刻选择车次并保留晚间活动。",
        )

    app = create_application(
        Settings(_env_file=None),
        model=ScriptedModel(
            [
                response("main", proposal, final_answer="查询前估算文字，不应直接作为最终回复"),
                review,
            ]
        ),
        storage_root=tmp_path,
        providers={"train": train},
    )
    result = await app.run(query)
    assert len(app.raw_model.messages) == 2
    assert result["status"] == "completed", result
    assert result["execution_facts"]["saved"]
    assert result["final_answer"].startswith("10月21日7点")
    assert len(train.calls) == 1
    saved = app.trips.load(result["trip"]["trip_plan_id"])
    assert saved["committed"] and saved["tasks"][0]["content"][0]["time"] == "晚上"


@pytest.mark.asyncio
async def test_default_application_does_not_restore_keyword_activity_rewriting(
    tmp_path, monkeypatch
):
    import trip_assistant.trips.service as service

    observed = []
    original = service.bind_user_times

    def checked(*args, **kwargs):
        observed.append(kwargs["enabled"])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "bind_user_times", checked)
    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-21"},
                "items": [{"domain": "train"}],
            }
        ],
    }
    app = create_application(
        Settings(_env_file=None),
        model=ScriptedModel(
            [
                response("main", plan),
                {"text": "候选已查询，尚未选择。"},
            ]
        ),
        storage_root=tmp_path,
        providers={"train": SimulatedTrainProvider()},
    )
    await app.run("后天重庆去上海，只安排单程火车")
    assert observed and not any(observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("change_activity", [True, False])
async def test_default_reply_is_bound_to_saved_train_and_activity(tmp_path, change_activity):
    from evals.manual.planning_reliability import FixtureTrain

    train = FixtureTrain()
    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-21"},
                "items": [{"domain": "train"}],
                "content": [
                    {
                        "date": "2026-10-21",
                        "time": "晚上",
                        "purpose": "滨江慢走",
                        "description": "抵达后沿江慢走",
                        "source": "proposal",
                        "strength": "soft",
                    }
                ],
            }
        ],
    }

    def review(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        view = trip["candidates"][0]
        selected = view["items"][1]
        assert selected["train_number"] == "G9002"
        task = {
            "task_id": trip["tasks"][0]["id"],
            "selection_refs": {
                "train": {
                    "query_id": view["query_id"],
                    "result_revision": view["result_revision"],
                    "candidate_id": selected["id"],
                }
            },
        }
        if change_activity:
            content = deepcopy(trip["tasks"][0]["content"])
            content[0]["purpose"] = "就近散步"
            content[0]["description"] = "在住处附近轻松散步"
            task.update(components=["train", "content"], content=content)
        return response(
            "review",
            {
                "operation": "amend" if change_activity else "select",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": [task],
            },
            reply={"explanation": "抵达后留出接驳余量，活动保持轻松。"},
        )

    app = create_application(
        Settings(_env_file=None),
        model=ScriptedModel(
            [
                response("main", plan),
                review,
            ]
        ),
        storage_root=tmp_path,
        providers={"train": train},
    )
    result = await app.run("后天从重庆去上海，只要单程，抵达后散步")
    assert len(app.raw_model.messages) == 2 and result["status"] == "completed", [
        (r.get("status"), r.get("reason"), r.get("error")) for r in result["tool_results"]
    ]
    assert "G9002" in result["final_answer"] and "15:20" in result["final_answer"]
    assert "14:20" not in result["final_answer"]
    purpose = "就近散步" if change_activity else "滨江慢走"
    assert purpose in result["final_answer"]
    saved = app.trips.load(result["trip"]["trip_plan_id"])
    assert saved["tasks"][0]["content"][0]["purpose"] == purpose
    assert len(train.calls) == 1


@pytest.mark.asyncio
async def test_invalid_reply_reference_rejects_whole_batch_before_memory_write(tmp_path):
    from support.native import NativeRuntimeFixture
    from test_goal_assessment import prepared_selection

    from evals.simulated_travel import SimulatedTrainProvider

    runtime = NativeRuntimeFixture(tmp_path, {"train": SimulatedTrainProvider()})
    plan = await prepared_selection(runtime)
    revision = runtime.trip_service.store.load(plan["trip_plan_id"])["revision"]
    runtime.model.script = [
        {
            "content": [
                {
                    "type": "tool_use",
                    "id": "memory",
                    "name": "write_memory",
                    "input": {
                        "request_kind": "planning",
                        "operation": "update",
                        "updates": [{"op": "add", "text": "喜欢轻松旅行"}],
                    },
                },
                {
                    "type": "tool_use",
                    "id": "bad",
                    "name": "write_trip",
                    "input": {
                        "request_kind": "planning",
                        "tripplan": plan,
                        "reply": {"tasks": {"unknown-task": ["train"]}},
                    },
                },
            ]
        },
        {"text": "引用无效，本批未保存。"},
    ]
    result = await runtime.run("保存车次并记住偏好")
    assert any(
        r.get("error", {}).get("code") == "INVALID_REPLY_REFERENCE" for r in result["tool_results"]
    )
    assert not runtime.memory.data.get("travel_memories")
    assert runtime.trip_service.store.load(plan["trip_plan_id"])["revision"] == revision


@pytest.mark.asyncio
async def test_budget_stop_reply_shows_actual_draft_not_unwritten_model_prose(tmp_path):
    from evals.manual.planning_reliability import FixtureTrain

    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "requires_hotel": False,
                "conditions": {"departure_date": "2026-10-21"},
                "items": [{"domain": "train"}],
                "content": [
                    {
                        "date": "2026-10-21",
                        "time": "晚上",
                        "purpose": "沿江散步",
                        "description": "抵达后沿江散步",
                        "source": "proposal",
                        "strength": "soft",
                    }
                ],
            }
        ],
    }
    app = create_application(
        Settings(_env_file=None, planning_rounds=1),
        model=ScriptedModel(
            [
                response("main", plan),
                {"text": "已经改为参观水族馆，G9002在14:20抵达，全部完成。"},
            ]
        ),
        storage_root=tmp_path,
        providers={"train": FixtureTrain()},
    )
    result = await app.run("后天重庆到上海单程，抵达后散步")
    assert result["stop_reason"] == "planning_round_limit_exceeded"
    assert len(app.raw_model.messages) == 1
    assert "沿江散步" in result["final_answer"] and "未完成" in result["final_answer"]
    assert "水族馆" not in result["final_answer"] and "全部完成" not in result["final_answer"]
    assert "14:20" not in result["final_answer"]


@pytest.mark.asyncio
async def test_free_text_cannot_finish_uncommitted_default_plan(tmp_path):
    from evals.manual.planning_reliability import FixtureTrain

    plan = {
        "operation": "propose",
        "default_round_trip": False,
        "tasks": [
            {
                "origin": "重庆",
                "destination": "上海",
                "conditions": {"departure_date": "2026-10-21"},
                "items": [{"domain": "train"}],
            }
        ],
    }
    app = create_application(
        Settings(_env_file=None),
        model=ScriptedModel(
            [response("main", plan), {"text": "已全部完成，改成水族馆并预订成功。"}]
        ),
        providers={"train": FixtureTrain()},
        storage_root=tmp_path,
    )
    result = await app.run("后天重庆到上海单程高铁")
    assert "未完成" in result["final_answer"]
    assert "水族馆" not in result["final_answer"] and "预订成功" not in result["final_answer"]
    assert not app.trips.load(result["trip"]["trip_plan_id"]).get("committed")
    assert len(app.raw_model.messages) == 2


@pytest.mark.asyncio
async def test_new_user_return_extension_starts_with_main_and_preserves_old_activities(tmp_path):
    from datetime import datetime

    from evals.manual.planning_reliability import FixtureTrain, seed_content
    from evals.simulated_travel import SimulatedHotelProvider
    from trip_assistant.agents.prompts import MAIN_PROMPT

    model = ScriptedModel([])
    app = create_application(
        Settings(_env_file=None),
        model=model,
        storage_root=tmp_path,
        providers={"train": FixtureTrain(), "hotel": SimulatedHotelProvider()},
    )
    seed = await seed_content(app, datetime.fromisoformat("2026-10-19T17:40:36+08:00"))
    refresh = app.refresh_memory_context

    def context(turn):
        turn["review_trip_plan_id"] = seed["id"]
        turn["current_time"] = "2026-10-19T17:40:36+08:00"
        refresh(turn)

    app.refresh_memory_context = context

    def amend(messages):
        assert messages[0]["content"] == MAIN_PROMPT
        trip = json.loads(messages[-1]["content"])["tripplan"]
        return response(
            "extend",
            {
                "operation": "amend",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": [
                    {
                        "task_id": trip["tasks"][0]["id"],
                        "components": ["hotel"],
                        "conditions": {"check_out": "2026-10-23", "nights": 3},
                        "field_sources": {"check_out": "user"},
                        "items": [{"domain": "hotel"}],
                    },
                    {
                        "task_id": trip["tasks"][1]["id"],
                        "components": ["train"],
                        "conditions": {"departure_date": "2026-10-23"},
                        "field_sources": {"departure_date": "user"},
                        "items": [{"domain": "train"}],
                    },
                ],
            },
        )

    def select(messages):
        trip = json.loads(messages[-1]["content"])["tripplan"]
        rows = []
        for task in trip["tasks"]:
            refs = {
                v["domain"]: {
                    "query_id": v["query_id"],
                    "result_revision": v["result_revision"],
                    "candidate_id": v["items"][0]["id"],
                }
                for v in trip["candidates"]
                if v["task_id"] == task["id"]
            }
            if refs:
                rows.append({"task_id": task["id"], "selection_refs": refs})
        return response(
            "choose",
            {
                "operation": "select",
                "trip_plan_id": trip["trip_plan_id"],
                "expected_revision": trip["revision"],
                "tasks": rows,
            },
            reply={},
        )

    model.script = [amend, select]
    result = await app.run("返程顺延一天，需要的住宿一起调整，其他保持原样")
    assert result["status"] == "completed" and result["stop_reason"] is None
    saved = app.trips.load(seed["id"])
    assert saved["tasks"][0]["content"] == seed["tasks"][0]["content"]
    assert saved["tasks"][0]["conditions"]["check_out"] == "2026-10-23"
    assert saved["tasks"][1]["conditions"]["departure_date"] == "2026-10-23"
    assert len(model.messages) == 2
