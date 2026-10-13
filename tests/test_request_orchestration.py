"""Production-entry regressions for the consolidated protocol replacing child turns."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_consolidated_travel_run import BatchMain
from test_information_agent_loop import Public
from test_information_tools import Provider
from workflow_runtime import OfflineProvider

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from agents.main_agent import MainAgent
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.tools import ToolExecutor


def query_harness(decision, memory=None, executor=None):
    calls = []

    async def raw(messages, **kwargs):
        calls.append(deepcopy(messages))
        return SimpleNamespace(text=json.dumps(decision, ensure_ascii=False))

    class Dormant(dict):
        def __getitem__(self, name):
            pytest.fail("request loaded a dormant agent: " + name)

    model = MeteredModel(raw, memory.session_store.append_run if memory else lambda r: None)
    harness = ExecutionHarness(MainAgent(model), Dormant(), memory)
    harness.runner.executor = executor or ToolExecutor({}, Public())
    return harness, calls


@pytest.mark.asyncio
async def test_weather_uses_one_main_call_and_direct_tool_result():
    harness, calls = query_harness(
        {
            "response_mode": "answer",
            "agent_schedule": [],
            "query_plan": [
                {"domain": "weather", "parameters": {"city": "北京", "date": "2026-10-10"}}
            ],
        }
    )
    run = RunState("t")
    result = await harness.run_turn(
        {
            "original_query": "北京天气",
            "recent_messages": [{"role": "assistant", "content": "PRIVATE_TRANSCRIPT"}],
        },
        run,
    )
    assert result["status"] == "ok" and result["finalization_method"] == "tools"
    assert len(calls) == 1 and run.external_request_count == 1
    assert "18" in result["final_answer"] and "PRIVATE_TRANSCRIPT" not in str(run.tool_requests)
    assert result["domain_results"]["weather"]["query"]["date"] == "2026-10-10"


@pytest.mark.asyncio
async def test_missing_tool_conditions_do_not_add_model_or_provider_call():
    harness, calls = query_harness(
        {
            "response_mode": "answer",
            "agent_schedule": [],
            "query_plan": [{"domain": "weather", "parameters": {}}],
        }
    )
    run = RunState("t")
    result = await harness.run_turn({"original_query": "天气"}, run)
    assert result["status"] == "needs_input" and len(calls) == 1 and run.external_request_count == 0


@pytest.mark.asyncio
async def test_mixed_preference_commit_is_reread_before_query_and_not_model_inferred(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    harness, calls = query_harness(
        {
            "response_mode": "answer",
            "agent_schedule": [],
            "query_plan": [
                {"domain": "weather", "parameters": {"city": "北京", "date": "2026-10-10"}}
            ],
        },
        memory,
    )
    result = await harness.run_turn(
        {"original_query": "以后出差尽量高铁，查北京天气"}, RunState("t")
    )
    assert result["status"] == "ok" and len(calls) == 1
    assert "以后出差尽量高铁" in calls[0][0]["content"]
    assert memory.long_term.data["travel_memories"][0]["text"] == "以后出差尽量高铁"
    assert not MemoryManager("bob", "s", str(tmp_path)).long_term.data.get("travel_memories")


@pytest.mark.asyncio
async def test_preference_save_failure_stops_dependent_main_and_tools(tmp_path, monkeypatch):
    memory = MemoryManager("alice", "s", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(memory.long_term, "apply_memory_updates", fail)
    harness, calls = query_harness(
        {"response_mode": "direct", "final_answer": "should not run"}, memory
    )
    run = RunState("t")
    result = await harness.run_turn({"original_query": "以后出差尽量高铁，查北京天气"}, run)
    assert result["status"] == "error" and not calls and run.external_request_count == 0
    assert not memory.long_term.data.get("travel_memories")


@pytest.mark.asyncio
async def test_pure_history_and_memory_commands_use_zero_models(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    harness, calls = query_harness({}, memory)
    for text in ("以后出差尽量高铁", "查看我的偏好", "查看我的历史行程"):
        result = await harness.run_turn({"original_query": text}, RunState(text))
        assert result["status"] == "ok"
    assert calls == []


@pytest.mark.asyncio
async def test_financial_answer_comes_from_provider_and_no_finalization_model():
    provider = Provider()
    harness, calls = query_harness(
        {
            "response_mode": "answer",
            "agent_schedule": [],
            "query_plan": [
                {
                    "domain": "hotel",
                    "parameters": {
                        "city": "北京",
                        "check_in": "2026-10-10",
                        "check_out": "2026-10-11",
                        "guests": 1,
                    },
                }
            ],
        },
        executor=ToolExecutor({"hotel_search": provider}),
    )
    result = await harness.run_turn({"original_query": "北京酒店"}, RunState("t"))
    assert "100" in result["final_answer"] and "已预订" not in result["final_answer"]
    assert len(provider.queries) == len(calls) == 1


@pytest.mark.asyncio
async def test_bad_batch_reply_uses_program_fallback_without_model_rewrite(tmp_path):
    class Main(BatchMain):
        async def select_batch(self, context):
            action = await super().select_batch(context)
            action["reply_template"] = "299999元，已预订成功。"
            return action

    main = Main()
    train, hotel = OfflineProvider("train"), OfflineProvider("hotel")
    harness = ExecutionHarness(main, {}, MemoryManager("alice", "s", str(tmp_path)))
    harness.runner.executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    result = await harness.run_turn(
        {"original_query": "重庆上海往返", "current_time": "2026-10-10T10:00:00+08:00"},
        RunState("t"),
    )
    assert result["status"] == "completed" and result["reply_style"] == "fallback"
    assert (
        main.calls == 2
        and "299999" not in result["final_answer"]
        and "已预订" not in result["final_answer"]
    )


@pytest.mark.asyncio
async def test_read_request_bypasses_main_and_preserves_business_revision(tmp_path):
    from workflow_support import populated_workflow

    memory = MemoryManager("alice", "s", str(tmp_path))
    saved = memory.workflow_store.save(populated_workflow(), expected_revision=None)
    harness, calls = query_harness({}, memory)
    run = RunState("t")
    result = await harness.run_turn(
        {
            "original_query": "读取",
            "read_request": {
                "workflow_id": saved["id"],
                "task_id": saved["tasks"][0]["id"],
                "domain": "train",
                "all": True,
            },
        },
        run,
    )
    assert result["status"] == "ok" and result["read_result"]["pages"][-1]["has_more"] is False
    assert not calls and run.external_request_count == 0
    assert memory.workflow_store.load(saved["id"]) == saved
    assert "G0" in result["final_answer"]


@pytest.mark.asyncio
async def test_read_cross_user_never_returns_saved_candidates(tmp_path):
    from workflow_support import populated_workflow

    alice = MemoryManager("alice", "s", str(tmp_path))
    saved = alice.workflow_store.save(populated_workflow(), expected_revision=None)
    harness, calls = query_harness({}, MemoryManager("bob", "s", str(tmp_path)))
    result = await harness.run_turn(
        {"original_query": "读取", "read_request": {"workflow_id": saved["id"]}}, RunState("t")
    )
    assert result["status"] == "partial" and "read_result" not in result and calls == []
