from copy import deepcopy

import pytest
from test_consolidated_travel_run import BatchMain
from workflow_runtime import OfflineProvider

from agents.contracts import RunState
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager
from travel_data.tools import ToolExecutor


@pytest.mark.asyncio
async def test_failed_amendment_preserves_commit_and_unknown_resume_never_replays(tmp_path):
    memory = MemoryManager("alice", "s", str(tmp_path))
    harness = ExecutionHarness(BatchMain(), {}, memory)
    harness.runner.executor = ToolExecutor(
        {"train_search": OfflineProvider("train"), "hotel_search": OfflineProvider("hotel")}
    )
    context = {"original_query": "安排往返", "current_time": "2026-10-10T10:00:00+08:00"}
    first = await harness.run_turn(context, RunState("first"))
    original = deepcopy(first["workflow"])
    task = original["tasks"][0]

    class Amend(BatchMain):
        async def initialize(self, context):
            return {
                "response_mode": "workflow",
                "agent_schedule": [],
                "resume_workflow_id": original["id"],
                "amendment_patch": {
                    "workflow_id": original["id"],
                    "expected_workflow_revision": original["revision"],
                    "task_id": task["id"],
                    "expected_task_revision": task["revision"],
                    "components": ["hotel"],
                    "condition_changes": {"hotel_quote_required": True},
                    "selection_refs": {},
                },
            }

    harness.main_agent = Amend()
    failed = await harness.run_turn(context, RunState("amend"))
    assert failed["status"] == "partial"
    saved = memory.workflow_store.load(original["id"])
    assert saved["tasks"] == original["tasks"] and saved["status"] == "completed"
    assert saved["pending_amendment"]["tasks"][0]["conditions"]["hotel_quote_required"]
    pending = saved["pending_amendment"]
    pending["execution_plan"][-1]["completed_at"] = None
    memory.workflow_store.save(saved, expected_revision=saved["revision"])

    class Resume(BatchMain):
        async def initialize(self, context):
            return {
                "response_mode": "workflow",
                "agent_schedule": [],
                "resume_workflow_id": original["id"],
            }

    train, hotel = OfflineProvider("train"), OfflineProvider("hotel")
    harness.main_agent = Resume()
    harness.runner.executor = ToolExecutor({"train_search": train, "hotel_search": hotel})
    resumed = await harness.run_turn(context, RunState("resume"))
    assert resumed["stop_reason"] == "request_completion_unknown"
    assert not train.calls and not hotel.calls
    assert memory.workflow_store.load(original["id"])["tasks"] == original["tasks"]
