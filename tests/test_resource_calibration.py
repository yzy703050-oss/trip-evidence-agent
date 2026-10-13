from types import SimpleNamespace
import pytest
from context.telemetry import MeteredModel, model_stage, model_scope


@pytest.mark.asyncio
async def test_success_budget_counts_completed_requests_not_errors_and_keeps_task_scope():
    from context.resource_budget import success_budget_scope, ModelCallLimitExceeded

    records = []
    attempts = 0

    async def raw(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("offline failure")
        return SimpleNamespace(text="ok")

    model = MeteredModel(raw, records.append)
    with success_budget_scope(main_per_turn=2, main_per_task=1) as budget, model_stage("main:step"):
        with model_scope(task_id="t1"):
            with pytest.raises(ValueError):
                await model([])
            await model([])
            with pytest.raises(ModelCallLimitExceeded):
                await model([])
        with model_scope(task_id="t2"):
            await model([])
        with pytest.raises(ModelCallLimitExceeded):
            await model([])
    assert budget.total == 2 and budget.by_task == {"t1": 1, "t2": 1}
    assert [r["status"] for r in records] == ["error", "success", "success"]


@pytest.mark.asyncio
async def test_calibration_releases_only_target_caps_and_records_same_call_context():
    from context.resource_budget import calibration_mode, success_budget_scope
    from context.context_budget import BudgetedModel

    records = []

    async def raw(*args, **kwargs):
        return SimpleNamespace(text="offline sample")

    model = BudgetedModel(MeteredModel(raw, records.append), budget_tokens=1, record=records.append)
    with (
        calibration_mode(),
        success_budget_scope(main_per_turn=1, main_per_task=1) as budget,
        model_stage("main:step"),
    ):
        await model([{"role": "user", "content": "测量样本"}])
        await model([{"role": "user", "content": "另一个样本"}])
    assert budget.total == 2
    estimates = [r for r in records if r["type"] == "context_budget"]
    assert [r["phase"] for r in estimates] == ["input", "output", "input", "output"]
    assert all(r["tokens"] > 1 and r["estimated"] for r in estimates)


@pytest.mark.asyncio
async def test_actual_scheduling_cap_counts_actions_not_task_count(tmp_path):
    from workflow_runtime import Runtime
    import config
    from unittest.mock import patch

    with patch.dict(config.WORKFLOW_LIMITS, {"main_decisions_per_turn": 3}):
        runtime = Runtime(tmp_path)
        result = await runtime.turn()
    assert result["status"] == "completed"
    decisions = [
        r for r in runtime.memory.session_store.read_runs() if r["type"] == "workflow_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0]["action"] == "commit_batch"


@pytest.mark.asyncio
async def test_child_failed_model_request_is_not_a_success_count(tmp_path):
    from workflow_runtime import Runtime

    runtime = Runtime(tmp_path)

    async def raw(*args, **kwargs):
        raise ValueError("offline failure")

    runtime.info.model.model = raw
    await runtime.turn()
    child_results = [
        e["content"]
        for e in runtime.memory.session_store.read_events()
        if e["type"] == "workflow_message" and e["content"].get("type") == "task_result"
    ]
    assert child_results == []
    assert runtime.info_model.calls == 0
    assert not [
        r for r in runtime.memory.session_store.read_runs() if r["type"] == "information_delegation"
    ]
