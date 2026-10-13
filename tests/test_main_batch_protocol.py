import json
from types import SimpleNamespace

import pytest

from agents.main_agent import MainAgent


@pytest.mark.asyncio
async def test_main_uses_one_batch_decision_without_tools_or_validate_reply_calls():
    calls = []

    async def model(messages, **kwargs):
        calls.append((messages, kwargs))
        return SimpleNamespace(
            text=json.dumps(
                {"action": "commit_batch", "expected_workflow_revision": 1, "draft_tasks": []}
            )
        )

    result = await MainAgent(model).select_batch(
        {"workflow": {"id": "w", "revision": 1, "tasks": []}, "task_results": []}
    )
    assert result["action"] == "commit_batch" and len(calls) == 1
    assert "tools" not in calls[0][1]


@pytest.mark.asyncio
async def test_main_amendment_can_use_all_saved_candidates_without_tool_call():
    async def model(messages, **kwargs):
        return SimpleNamespace(
            text='{"workflow_id":"w","expected_workflow_revision":1,"task_id":"t","expected_task_revision":1,"components":["hotel"],"condition_changes":{},"selection_refs":{}}'
        )

    patch = await MainAgent(model).amend_saved_trip(
        {"workflow": {"id": "w", "revision": 1, "tasks": []}}
    )
    assert patch["components"] == ["hotel"]
