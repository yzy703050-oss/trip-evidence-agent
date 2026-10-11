"""A new CLI session must match a trip before persisting another workflow."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from workflow_runtime import Runtime, WorkflowMain
from workflow_support import proposal

from agents.main_agent import MainAgent
from context.memory_manager import MemoryManager


class MatchingMain(WorkflowMain):
    def __init__(self, p=None, match=None, before_match=None):
        super().__init__(p)
        self.match = match or {"action": "new"}
        self.before_match = before_match
        self.match_inputs = []
        self.agent = MainAgent(self.model)

    async def model(self, messages, **kwargs):
        self.match_inputs.append(json.loads(messages[-1]["content"]))
        if self.before_match:
            self.before_match()
        return SimpleNamespace(text=json.dumps(self.match, ensure_ascii=False))

    async def match_workflow(self, context):
        return await self.agent.match_workflow(context)


@pytest.mark.asyncio
async def test_three_new_sessions_reuse_completed_trip_without_requery(tmp_path):
    first = await Runtime(tmp_path, session="one").turn()
    for session in ("two", "three"):
        main = MatchingMain(match={"action": "reuse", "workflow_id": first["workflow_id"]})
        current = Runtime(tmp_path, session=session, main=main)
        result = await current.turn("上海到北京再杭州再上海，安排火车酒店")
        assert result["workflow_id"] == first["workflow_id"]
        assert result["status"] == "completed"
        assert result["workflow_revision"] == first["workflow_revision"]
        assert "已有规划" in result["final_answer"] and "并非实时信息" in result["final_answer"]
        assert not current.train.calls and not current.hotel.calls and not main.actions
        assert len(current.memory.workflow_store.list_all()) == 1
        assert current.memory.get_active_workflows()[0]["id"] == first["workflow_id"]
        candidate = main.match_inputs[0]["candidates"][0]
        assert candidate["tasks"][0]["destination"] == "北京"
        assert candidate["tasks"][0]["conditions"]["departure_date"] == "2026-10-11"
        assert candidate["created_at"] and candidate["updated_at"]
        assert "candidate_cache" not in candidate and "results_by_query" not in candidate


@pytest.mark.asyncio
async def test_matching_partial_trip_continues_same_tasks(tmp_path):
    original = Runtime(tmp_path, session="one")
    original.main.finish_without_check = True
    first = await original.turn()
    main = MatchingMain(match={"action": "reuse", "workflow_id": first["workflow_id"]})
    current = Runtime(tmp_path, session="two", main=main)
    result = await current.turn("上海到北京再杭州再上海，安排火车酒店")
    assert result["workflow_id"] == first["workflow_id"]
    assert result["status"] == "completed"
    assert [t["id"] for t in result["workflow"]["tasks"]] == [
        t["id"] for t in first["workflow"]["tasks"]
    ]
    assert len(current.memory.workflow_store.list_all()) == 1


@pytest.mark.asyncio
async def test_ambiguous_match_asks_without_creating_or_mutating(tmp_path):
    await Runtime(tmp_path, session="one").turn()
    await Runtime(tmp_path, session="two").turn()
    main = MatchingMain(
        match={"action": "clarify", "question": "找到两份相同日期路线的规划，要继续哪一份？"}
    )
    current = Runtime(tmp_path, session="three", main=main)
    before = current.memory.workflow_store.list_all()
    result = await current.turn()
    assert result["status"] == "needs_input"
    assert result["stop_reason"] == "workflow_match_required"
    assert "两份" in result["final_answer"]
    assert current.memory.workflow_store.list_all() == before
    assert not current.train.calls and not current.hotel.calls


@pytest.mark.asyncio
async def test_changed_relative_date_can_create_new_trip(tmp_path):
    await Runtime(tmp_path, session="one").turn()
    changed = deepcopy(proposal())
    changed["confirmed_conditions"]["start_date"] = "2026-10-12"
    changed["tasks"][0]["conditions"]["departure_date"] = "2026-10-12"
    main = MatchingMain(changed, match={"action": "new"})
    current = Runtime(tmp_path, session="two", main=main)
    result = await current.turn("明天去北京再杭州再上海，安排火车酒店")
    assert len(current.memory.workflow_store.list_all()) == 2
    assert result["workflow"]["tasks"][0]["conditions"]["departure_date"] == "2026-10-12"
    assert main.match_inputs[0]["current_time"] == "2026-10-10T10:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "match",
    [
        {"action": "reuse", "workflow_id": "workflow_foreign"},
        {"action": "reuse"},
        {"action": "broken"},
    ],
)
async def test_invalid_model_match_cannot_create_duplicate_or_select_unknown_trip(tmp_path, match):
    await Runtime(tmp_path).turn()
    current = Runtime(tmp_path, session="new", main=MatchingMain(match=match))
    before = current.memory.workflow_store.list_all()
    result = await current.turn()
    assert result["status"] == "error"
    assert current.memory.workflow_store.list_all() == before
    assert not current.train.calls


@pytest.mark.asyncio
async def test_matching_checks_revision_again_before_using_plan(tmp_path):
    original = Runtime(tmp_path)
    first = await original.turn()

    def competing_save():
        w = original.memory.workflow_store.load(first["workflow_id"])
        original.memory.workflow_store.save(w, expected_revision=w["revision"])

    main = MatchingMain(
        match={"action": "reuse", "workflow_id": first["workflow_id"]}, before_match=competing_save
    )
    current = Runtime(tmp_path, session="new", main=main)
    result = await current.turn()
    assert result["stop_reason"] == "workflow_revision_conflict"
    assert not current.train.calls
    assert len(current.memory.workflow_store.list_all()) == 1


@pytest.mark.asyncio
async def test_other_users_workflows_are_not_compared(tmp_path):
    await Runtime(tmp_path).turn()
    main = MatchingMain()
    current = Runtime(tmp_path, session="bob", main=main)
    current.memory = MemoryManager("bob", "bob", str(tmp_path))
    current.harness.memory_manager = current.memory
    result = await current.turn()
    assert result["status"] == "completed"
    assert main.match_inputs == []
    assert len(current.memory.workflow_store.list_all()) == 1


@pytest.mark.asyncio
async def test_match_question_survives_resume_and_numbered_selection_uses_saved_plan(tmp_path):
    first = await Runtime(tmp_path, session="one").turn()
    second = await Runtime(tmp_path, session="two").turn()
    main = MatchingMain(match={"action": "clarify", "question": "第一份或第二份，你要继续哪份？"})
    await Runtime(tmp_path, session="choosing", main=main).turn()
    current = Runtime(tmp_path, session="choosing")

    async def initialize(context):
        pending = context["pending_workflow_match"]
        selected = pending["candidates"][0]["id"]
        assert selected == second["workflow_id"]  # Store lists latest updated first.
        assert {w["id"] for w in context["known_workflows"]} == {
            first["workflow_id"],
            second["workflow_id"],
        }
        return {"response_mode": "workflow", "agent_schedule": [], "resume_workflow_id": selected}

    current.main.initialize = initialize
    result = await current.turn("第一份")
    assert result["workflow_id"] == second["workflow_id"]
    assert result["workflow_revision"] == second["workflow_revision"]
    assert not current.train.calls and not current.hotel.calls
    assert "pending_workflow_match" not in current.memory.session_store.read_state()


@pytest.mark.asyncio
async def test_refresh_keeps_workflow_identity_but_requeries_tools(tmp_path):
    first = await Runtime(tmp_path).turn()
    main = MatchingMain(
        match={"action": "reuse", "workflow_id": first["workflow_id"], "refresh": True}
    )
    current = Runtime(tmp_path, session="new", main=main)
    result = await current.turn("重新查询这趟旅行最新车票酒店")
    assert result["workflow_id"] == first["workflow_id"]
    assert result["workflow_revision"] > first["workflow_revision"]
    assert current.train.calls and current.hotel.calls
    assert len(current.memory.workflow_store.list_all()) == 1


@pytest.mark.asyncio
async def test_reused_simulated_plan_keeps_simulation_disclosure(tmp_path):
    original = Runtime(tmp_path)
    first = await original.turn()
    saved = original.memory.workflow_store.load(first["workflow_id"])
    for row in saved["results_by_query"].values():
        row["source"]["provider"] = "simulation:test"
    original.memory.workflow_store.save(saved, expected_revision=saved["revision"])
    current = Runtime(
        tmp_path,
        session="new",
        main=MatchingMain(match={"action": "reuse", "workflow_id": saved["id"]}),
    )
    result = await current.turn()
    assert result["data_mode"] == "simulation"
    assert "模拟" in result["final_answer"]
