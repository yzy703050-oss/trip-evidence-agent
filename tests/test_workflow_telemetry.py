import pytest
from context.telemetry import MeteredModel, model_scope
from types import SimpleNamespace
from agents.contracts import RunState
from workflow_runtime import Runtime


@pytest.mark.asyncio
async def test_initial_proposal_is_available_for_failure_diagnosis(tmp_path):
    r = Runtime(tmp_path)
    await r.turn()
    plans = [e for e in r.memory.session_store.read_runs() if e['type'] == 'agent_plan']
    assert plans[0]['decision']['workflow_proposal']['tasks'][0]['origin'] == '上海'
