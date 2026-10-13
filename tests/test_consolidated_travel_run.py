from copy import deepcopy

import pytest
from workflow_runtime import OfflineProvider

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager


class BatchMain:
    def __init__(self):
        self.calls = 0
        self.views = []

    async def initialize(self, context):
        self.calls += 1
        return {
            "response_mode": "workflow",
            "agent_schedule": [],
            "workflow_proposal": {
                "confirmed_conditions": {},
                "tasks": [
                    {
                        "origin": "重庆",
                        "destination": "上海",
                        "purpose": "visit",
                        "requires_hotel": True,
                        "conditions": {
                            "departure_date": "2026-10-11",
                            "check_in": "2026-10-11",
                            "check_out": "2026-10-14",
                        },
                    },
                    {
                        "origin": "上海",
                        "destination": "重庆",
                        "purpose": "return",
                        "requires_hotel": False,
                        "conditions": {"departure_date": "2026-10-14"},
                    },
                ],
            },
        }

    async def select_batch(self, context):
        self.calls += 1
        self.views.append(deepcopy(context))
        tasks = []
        for task in context["workflow"]["tasks"]:
            refs = {}
            for row in context["task_results"]:
                if row.get("task_id") == task["id"] and row.get("items"):
                    refs[row["domain"]] = {
                        "query_id": row.get("query_id") or row["id"],
                        "result_revision": row["result_revision"],
                        "candidate_id": row["items"][0]["id"],
                    }
            tasks.append(
                {
                    "task_id": task["id"],
                    "task_revision": task["revision"],
                    "draft_plan": {
                        "task_revision": task["revision"],
                        "train_selection": refs.get("train"),
                        "hotel_selection": refs.get("hotel"),
                        "schedule": {
                            k: v
                            for k, v in task["conditions"].items()
                            if k in {"check_in", "check_out"}
                        },
                        "unverified_requirements": [],
                    },
                }
            )
        return {
            "action": "commit_batch",
            "expected_workflow_revision": context["workflow"]["revision"],
            "draft_tasks": tasks,
            "reply_template": "这趟出行可以这样安排。",
        }


@pytest.mark.asyncio
async def test_normal_tourism_two_main_calls_and_information_agent_never_loaded(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    main = BatchMain()
    train = OfflineProvider("train")
    hotel = OfflineProvider("hotel")

    class Registry(dict):
        providers = {"train_search": train, "hotel_search": hotel}

        def __getitem__(self, key):
            pytest.fail(f"no legacy agent: {key}")

    run = RunState("t")
    result = await ExecutionHarness(main, Registry(), memory).run_turn(
        {"original_query": "安排重庆上海旅游往返", "current_time": "2026-10-10T10:00:00+08:00"}, run
    )
    assert result["status"] == "completed", result
    assert main.calls == 2 and len(train.calls) == 2 and len(hotel.calls) == 1
    assert run.external_request_count == 3
    assert all(t["status"] == "validated" for t in result["workflow"]["tasks"])
    assert all(r.get("completed_at") for r in result["workflow"]["execution_plan"])
    assert memory.workflow_store.load(result["workflow_id"]) == result["workflow"]


@pytest.mark.asyncio
async def test_missing_origin_ends_turn_without_tool_or_selection_model(tmp_path):
    class Main(BatchMain):
        async def initialize(self, context):
            value = await super().initialize(context)
            value["workflow_proposal"]["tasks"][0]["origin"] = None
            return value

    main = Main()
    memory = MemoryManager("a", "s", str(tmp_path))
    result = await ExecutionHarness(main, {}, memory).run_turn(
        {"original_query": "去上海旅游"}, RunState("t")
    )
    assert result["status"] == "needs_input"
    assert result["workflow"]["status"] == "running"
    assert main.calls == 1


@pytest.mark.asyncio
async def test_mixed_preference_updated_before_schedule(tmp_path):
    class Main:
        async def initialize(self, context):
            assert context["effective_preferences"]["memories"][0]["text"] == "以后出差尽量高铁"
            return {"response_mode": "direct", "agent_schedule": [], "final_answer": "收到"}

    memory = MemoryManager("a", "s", str(tmp_path))
    result = await ExecutionHarness(Main(), {}, memory).run_turn(
        {"original_query": "以后出差尽量高铁，帮我规划旅游"}, RunState("t")
    )
    assert result["status"] == "ok"
