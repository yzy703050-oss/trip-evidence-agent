import importlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_information_agent_loop import Public, info_class
from test_main_harness import Info, Main

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from agents.main_agent import MainAgent
from context.context_budget import BudgetedModel
from context.memory_manager import MemoryManager
from travel_data.tools import ToolExecutor




@pytest.mark.asyncio
async def test_main_request_rebuilds_from_compacted_complete_old_history(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(6):
        turn = memory.start_turn(f"old{index}" + "旧需求" * 150)
        memory.record_message("assistant", "旧答复" * 150, turn, final=True)
    memory.start_turn("本轮简短请求")
    inputs = []

    async def model(messages, **kwargs):
        inputs.append(deepcopy(messages))
        if "待压缩主消息" in messages[0]["content"]:
            return SimpleNamespace(text="已讨论完整旧需求")
        return SimpleNamespace(text='{"action":"answer","final_answer":"ok"}')

    wrapped = BudgetedModel(model, budget_tokens=3500)
    memory.llm_model = wrapped
    main = MainAgent(wrapped, memory_manager=memory)
    context = {"original_query": "本轮简短请求"}
    turn = await main._request(
        lambda view: [{"role": "user", "content": json.dumps(view, ensure_ascii=False)}],
        context=context,
        stage="main",
    )
    assert json.loads(turn.text)["final_answer"] == "ok"
    assert len(inputs) == 2
    actual = json.loads(inputs[-1][0]["content"])
    assert actual["session_summary"] == "已讨论完整旧需求"
    assert actual["recent_messages"][0]["content"].startswith("old")
    assert not actual["recent_messages"][0]["content"].startswith("old0")
    assert memory.session_store.read_state()["covered_through_seq"] % 2 == 0


def sample_result():
    return {
        "status": "partial",
        "summary": "已查到候选，房价未核实",
        "missing_fields": ["price"],
        "raw": "PRIVATE_TOOL_MARKER",
        "execution": {"payload": "PRIVATE_TOOL_MARKER"},
        "domain_results": {
            "train": {
                "status": "ok",
                "source": {
                    "provider": "train",
                    "fetched_at": "2026-10-11T00:00:00+00:00",
                    "url": "https://example.com",
                },
                "items": [
                    {
                        "id": "c1",
                        "train_number": "G1",
                        "price_cny": "100",
                        "raw": "PRIVATE_TOOL_MARKER",
                    }
                ],
            }
        },
    }


def test_child_projection_preserves_candidate_ids_and_sources_without_mutation():
    projection = importlib.import_module("agents.context_projection")
    original = sample_result()
    saved = deepcopy(original)
    view = projection.project_child_result(original)
    assert "PRIVATE_TOOL_MARKER" not in json.dumps(view)
    assert view["domain_results"]["train"]["items"][0]["id"] == "c1"
    assert view["domain_results"]["train"]["source"]["provider"] == "train"
    assert view["status"] == "partial"
    assert view["missing_fields"] == ["price"]
    assert original == saved


@pytest.mark.asyncio
async def test_child_raw_tool_marker_absent_from_main_requests_and_summary(tmp_path):
    inputs = []
    answers = [
        {
            "response_mode": "direct",
            "finalization_mode": "synthesize",
            "agent_schedule": [],
            "final_answer": "ok",
        },
        {"action": "finish", "status": "partial", "final_answer": "ok"},
        {"action": "answer", "final_answer": "ok"},
    ]

    async def model(messages, **kwargs):
        inputs.append(deepcopy(messages))
        return SimpleNamespace(text=json.dumps(answers.pop(0)))

    main = MainAgent(model)
    context = {
        "original_query": "查车票",
        "results": [{"agent_name": "information_query", "data": sample_result()}],
        "domain_results": sample_result()["domain_results"],
        "latest_child_result": sample_result(),
        "active_workflows": [
            {
                "id": "w1",
                "status": "partial",
                "tasks": [],
                "candidate_cache": {"x": "PRIVATE_TOOL_MARKER"},
            }
        ],
    }
    await main.initialize(context)
    await main.step(context)
    await main.finalize(context)
    assert "PRIVATE_TOOL_MARKER" not in json.dumps(inputs)
    assert "c1" in json.dumps(inputs)

    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(3):
        turn = memory.start_turn("查询" * 50)
        memory.record_tool_call(
            [{"id": f"tool{index}", "name": "search", "arguments": {}}], turn, "agent:info"
        )
        memory.record_tool_result(f"tool{index}", "PRIVATE_TOOL_MARKER", turn, "agent:info")
        memory.record_message(
            "assistant",
            json.dumps({"final_answer": "已查票", "results": sample_result()}),
            turn,
            final=True,
            metadata={"format": "main_result"},
        )
    summarized = []

    def summarize(old, events):
        summarized.extend(events)
        return "已查票"

    memory.compact_if_needed(
        130, summarizer=summarize, token_counter=lambda e: len(str(e.get("content", "")))
    )
    assert summarized
    assert "PRIVATE_TOOL_MARKER" not in json.dumps(summarized)


@pytest.mark.asyncio
async def test_reused_child_starts_fresh_messages_and_never_compacts():
    inputs = []

    class Memory:
        def compact_if_needed(self, *args, **kwargs):
            raise AssertionError("child must not compact")

        async def compact_if_needed_async(self, *args, **kwargs):
            raise AssertionError("child must not compact")

    async def model(messages, **kwargs):
        inputs.append(deepcopy(messages))
        return SimpleNamespace(text='{"summary":"完成"}')

    child = info_class()(
        model=model, memory_manager=Memory(), tool_executor=ToolExecutor({}, Public())
    )
    await child.run(
        {"original_query": "FIRST_PRIVATE_TASK", "requested_domains": []}, RunState("a")
    )
    await child.run({"original_query": "SECOND_TASK", "requested_domains": []}, RunState("b"))
    assert "FIRST_PRIVATE_TASK" in json.dumps(inputs[0])
    assert "FIRST_PRIVATE_TASK" not in json.dumps(inputs[1])
    assert "SECOND_TASK" in json.dumps(inputs[1])
    assert len(inputs[1]) == 2
