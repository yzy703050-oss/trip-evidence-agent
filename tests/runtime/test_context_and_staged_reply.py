import json

import pytest
from support.native import ScriptedModel

from trip_assistant.bootstrap import create_application
from trip_assistant.settings import Settings
from trip_assistant.tools.registry import build_registry


def test_already_injected_preferences_are_not_a_public_read_resource():
    tool = build_registry({})["read"]
    assert (
        "preferences" not in tool.schema["function"]["parameters"]["properties"]["resource"]["enum"]
    )
    with pytest.raises(ValueError):
        tool.validate({"request_kind": "lookup", "resource": "preferences"})


@pytest.mark.asyncio
async def test_actual_place_provider_capability_is_visible_without_extra_read(tmp_path):
    from evals.simulated_travel import SimulatedHotelProvider

    model = ScriptedModel(["你好"])
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={"hotel": SimulatedHotelProvider()},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    await app.run("你好")
    snapshot = next(
        m["content"]
        for m in model.messages[0][0]
        if m["role"] == "system" and m["content"].startswith("当前上下文")
    )
    assert "provider_capabilities" not in snapshot
    tools = model.messages[0][1]["tools"]
    assert any(
        "hotel_place" in t["function"]["description"]
        and "room_quotes" in t["function"]["description"]
        for t in tools
    )
    assert len(model.messages) == 1


@pytest.mark.asyncio
async def test_all_active_memory_and_summary_are_in_first_context(tmp_path):
    model = ScriptedModel(["你好"])
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    app.preferences.apply_memory_updates([{"op": "add", "text": f"偏好{i}"} for i in range(7)])
    app.preferences.save_analysis(
        {
            "memories": [{"text": "历史总结：住宿偏安静", "evidence_trip_ids": ["t0"]}],
            "covered_trip_ids": ["t0", "t1", "t2"],
        }
    )
    await app.run("你好")
    text = json.dumps(model.messages[0][0], ensure_ascii=False)
    assert "偏好6" in text and "memory_revision" in text
    assert '"preference_summary": "历史总结：住宿偏安静"' in text.replace('\\"', '"')
    assert len(model.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_memory_write_refreshes_injected_snapshot_without_read(tmp_path, partial):
    from types import SimpleNamespace

    model = ScriptedModel(
        [
            SimpleNamespace(
                content=[
                    {
                        "type": "tool_use",
                        "id": "m",
                        "name": "write_memory",
                        "input": {
                            "request_kind": "lookup",
                            "updates": [{"op": "add", "text": "以后出差尽量坐高铁"}]
                            + ([{"op": "retract", "id": "missing"}] if partial else []),
                        },
                    }
                ]
            ),
            "已记住。",
        ]
    )
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    await app.run("记住以后出差尽量坐高铁")
    second = json.dumps(model.messages[1][0], ensure_ascii=False)
    assert "以后出差尽量坐高铁" in second
    snapshot = next(
        m
        for m in model.messages[1][0]
        if m["role"] == "system" and m["content"].startswith("当前上下文")
    )
    assert "以后出差尽量坐高铁" in snapshot["content"]
    assert [
        e["tool_calls"][0]["name"] for e in app.sessions.read_events() if e["type"] == "tool_call"
    ] == ["write_memory"]


@pytest.mark.asyncio
async def test_successful_selection_uses_staged_answer_without_another_model_call(tmp_path):
    from types import SimpleNamespace

    from support.native import NativeRuntimeFixture

    runtime = NativeRuntimeFixture(tmp_path)

    def tool_response(call):
        identity, name, args = call
        return SimpleNamespace(
            content=[{"type": "tool_use", "id": identity, "name": name, "input": args}]
        )

    from trip_assistant.tools.registry import ToolDefinition, TripArguments

    async def write(args, turn):
        return {
            "status": "completed",
            "operation": "select",
            "trip_plan_id": "trip",
            "revision": 2,
            "validation": {"valid": True, "reconstructed_tasks": []},
        }

    runtime.registry["write_trip"] = ToolDefinition(
        "write_trip", "", "complex_write", TripArguments, write
    )
    runtime.model.script = [
        tool_response(
            (
                "w",
                "write_trip",
                {
                    "request_kind": "planning",
                    "tripplan": {
                        "operation": "select",
                        "trip_plan_id": "trip",
                        "expected_revision": 1,
                    },
                    "goal_assessment": {
                        "status": "satisfied",
                        "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                    },
                    "final_answer": "行程已保存，尚未预订。",
                },
            )
        )
    ]
    result = await runtime.run("选择并保存行程")
    assert result["status"] == "completed"
    assert result["final_answer"] == "行程已保存，尚未预订。"
    assert len(runtime.model.messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "rejected", "partial", "needs_input"])
async def test_failed_selection_discards_staged_success_and_observes_result(tmp_path, status):
    from types import SimpleNamespace

    from support.native import NativeRuntimeFixture

    from trip_assistant.tools.registry import ToolDefinition, TripArguments

    runtime = NativeRuntimeFixture(tmp_path)

    async def write(args, turn):
        return {"status": status, "reason": "candidate version changed"}

    runtime.registry["write_trip"] = ToolDefinition(
        "write_trip", "", "complex_write", TripArguments, write
    )
    runtime.model.script = [
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "w",
                    "name": "write_trip",
                    "input": {
                        "request_kind": "planning",
                        "tripplan": {
                            "operation": "select",
                            "trip_plan_id": "trip",
                            "expected_revision": 1,
                        },
                        "goal_assessment": {
                            "status": "satisfied",
                            "reason": "本测试模型已对照原请求与当前选择确认目标满足。",
                        },
                        "final_answer": "行程已保存。",
                    },
                }
            ]
        ),
        "候选发生变化，尚未保存这次修改。",
    ]
    result = await runtime.run("选择并保存行程")
    assert len(runtime.model.messages) == 2
    assert result["final_answer"] == "候选发生变化，尚未保存这次修改。"
    observation = json.loads(runtime.model.messages[1][0][-1]["content"])
    assert observation["status"] == status
    assert not runtime.sessions.pending_tool_calls()


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [False, True])
async def test_retraction_removes_memory_from_next_injected_snapshot(tmp_path, history):
    from types import SimpleNamespace

    model = ScriptedModel([])
    app = create_application(
        Settings(_env_file=None, single_pass_planning=False),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    if history:
        app.preferences.save_analysis(
            {
                "memories": [{"text": "以后都住全季", "evidence_trip_ids": ["t0"]}],
                "covered_trip_ids": ["t0", "t1", "t2"],
            }
        )
    else:
        app.preferences.apply_memory_updates([{"op": "add", "text": "以后都住全季"}])
    memory_id = app.preferences.data["travel_memories"][0]["id"]
    before = app.preferences.data["_revision"]
    model.script = [
        SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": "m",
                    "name": "write_memory",
                    "input": {
                        "request_kind": "lookup",
                        "updates": [{"op": "retract", "id": memory_id}],
                    },
                }
            ]
        ),
        "已删除这条偏好。",
    ]
    await app.run("删除全季偏好")
    snapshot = next(
        json.loads(m["content"].split("：", 1)[1])
        for m in model.messages[1][0]
        if m["role"] == "system" and m["content"].startswith("当前上下文")
    )
    assert snapshot["memories"] == []
    assert snapshot["preference_summary"] == ""
    assert snapshot["memory_revision"] > before
