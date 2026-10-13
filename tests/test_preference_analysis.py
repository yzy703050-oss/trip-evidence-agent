import json
from types import SimpleNamespace

import pytest

from context.memory_manager import MemoryManager


def samples(count):
    return [
        {"trip_plan_id": f"t{i}", "status": "completed", "selection_source": "user"}
        for i in range(count)
    ]


def test_three_unique_samples_bootstrap_and_persisted_marker(tmp_path):
    from agents.preference_analysis import get_analysis_trigger, save_preference_analysis

    memory = MemoryManager("a", "s", str(tmp_path))
    assert get_analysis_trigger(samples(2), {}) is None
    assert get_analysis_trigger(samples(3) + samples(3), {}) == "bootstrap"
    save_preference_analysis(
        memory, {"memories": [], "covered_trip_ids": ["t0", "t1", "t2"]}, "bootstrap"
    )
    restored = MemoryManager("a", "new", str(tmp_path))
    state = restored.long_term.data["preference_analysis"]
    assert state["initial_summary_done"] is True
    assert get_analysis_trigger(samples(10), state) is None
    assert (
        get_analysis_trigger(samples(10), state, explicit_reanalysis_requested=True)
        == "user_requested"
    )


def test_invalid_samples_excluded_and_save_atomic(tmp_path, monkeypatch):
    from agents.preference_analysis import save_preference_analysis, select_analysis_samples

    bad = [
        {"trip_plan_id": "cancel", "status": "completed", "stop_reason": "cancelled"},
        {"trip_plan_id": "sim", "status": "completed", "data_mode": "simulation"},
        {"trip_plan_id": "fail", "status": "error"},
    ]
    assert len(select_analysis_samples(samples(2) + samples(2) + bad)) == 2
    memory = MemoryManager("a", "s", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr(memory.long_term, "apply_memory_updates", fail)
    with pytest.raises(OSError):
        save_preference_analysis(memory, {"memories": [], "covered_trip_ids": ["t0"]}, "bootstrap")
    assert not memory.long_term.data.get("preference_analysis")


@pytest.mark.asyncio
async def test_analysis_preserves_conditions_sources_and_rejects_fake_evidence(tmp_path):
    from agents.memory_tools import apply_preference_updates, read_memory
    from agents.preference_analysis import PreferenceAnalysisAgent, save_preference_analysis

    async def model(messages):
        return SimpleNamespace(
            text=json.dumps(
                {
                    "memories": [
                        {"text": "独自出差时倾向靠近会场，尚不确定", "evidence_trip_ids": ["t0"]}
                    ],
                    "covered_trip_ids": ["t0", "t1", "t2"],
                },
                ensure_ascii=False,
            )
        )

    memory = MemoryManager("a", "s", str(tmp_path))
    apply_preference_updates(memory, [{"op": "add", "text": "带孩子旅行时安静酒店"}])
    result = await PreferenceAnalysisAgent(model).analyze({"history": samples(3), "memories": []})
    save_preference_analysis(memory, result, "bootstrap")
    assert [m["source"] for m in read_memory(memory, {})["memories"]] == ["explicit", "history"]
    assert "尚不确定" in read_memory(memory, {})["memories"][1]["text"]

    async def bad(messages):
        return SimpleNamespace(
            text='{"memories":[{"text":"高铁","evidence_trip_ids":["invented"]}],"covered_trip_ids":["invented"]}'
        )

    with pytest.raises(ValueError):
        await PreferenceAnalysisAgent(bad).analyze({"history": samples(3)})


@pytest.mark.asyncio
async def test_real_request_bootstrap_once_after_three_plans_and_reopen(tmp_path):
    from workflow_runtime import Runtime, WorkflowMain

    from context.telemetry import MeteredModel

    for number in range(3):
        assert (await Runtime(tmp_path, session=f"plan{number}").turn())["status"] == "completed"
    runtime = Runtime(tmp_path, session="bootstrap")
    ids = [w["id"] for w in runtime.memory.workflow_store.list_summaries()]
    calls = []

    async def model(messages):
        calls.append(messages)
        return SimpleNamespace(text=json.dumps({"memories": [], "covered_trip_ids": ids}))

    class Main(WorkflowMain):
        pass

    main = Main()
    main.model = MeteredModel(model, runtime.memory.session_store.append_run)
    runtime.harness.main_agent = main
    assert (await runtime.turn())["status"] == "completed"
    assert len(calls) == 1
    assert runtime.memory.long_term.data["preference_analysis"]["initial_summary_done"]
    records = [r for r in runtime.memory.session_store.read_runs() if r["type"] == "model_call"]
    assert records[0]["stage"] == "agent:preference_analysis" and records[0]["turn_id"]
    reopened = Runtime(tmp_path, session="later")
    reopened.harness.main_agent = main
    assert (await reopened.turn())["status"] == "completed"
    assert len(calls) == 1
