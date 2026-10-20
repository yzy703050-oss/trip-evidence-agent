import json
from types import SimpleNamespace

import pytest

from trip_assistant.storage.preferences import LongTermMemory


def samples(count):
    return [
        {"trip_plan_id": f"t{i}", "status": "completed", "selection_source": "user"}
        for i in range(count)
    ]


def test_three_unique_samples_bootstrap_and_persisted_marker(tmp_path):
    from trip_assistant.agents.preference import get_analysis_trigger, save_preference_analysis

    memory = LongTermMemory("a", str(tmp_path))
    assert get_analysis_trigger(samples(2), {}) is None
    assert get_analysis_trigger(samples(3) + samples(3), {}) == "bootstrap"
    save_preference_analysis(
        memory, {"memories": [], "covered_trip_ids": ["t0", "t1", "t2"]}, "bootstrap"
    )
    restored = LongTermMemory("a", str(tmp_path))
    state = restored.data["preference_analysis"]
    assert state["initial_summary_done"] is True
    assert get_analysis_trigger(samples(10), state) == "incremental"
    assert (
        get_analysis_trigger(samples(10), state, explicit_reanalysis_requested=True)
        == "user_requested"
    )


def test_invalid_samples_excluded_and_save_atomic(tmp_path, monkeypatch):
    from trip_assistant.agents.preference import save_preference_analysis, select_analysis_samples

    bad = [
        {"trip_plan_id": "cancel", "status": "completed", "stop_reason": "cancelled"},
        {"trip_plan_id": "sim", "status": "completed", "data_mode": "simulation"},
        {"trip_plan_id": "fail", "status": "error"},
    ]
    assert len(select_analysis_samples(samples(2) + samples(2) + bad)) == 2
    memory = LongTermMemory("a", str(tmp_path))

    def fail(*args, **kwargs):
        raise OSError("save failed")

    monkeypatch.setattr(memory, "apply_memory_updates", fail)
    with pytest.raises(OSError):
        save_preference_analysis(memory, {"memories": [], "covered_trip_ids": ["t0"]}, "bootstrap")
    assert not memory.data.get("preference_analysis")


@pytest.mark.asyncio
async def test_analysis_preserves_conditions_sources_and_rejects_fake_evidence(tmp_path):
    from trip_assistant.agents.preference import PreferenceAnalysisAgent, save_preference_analysis

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

    memory = LongTermMemory("a", str(tmp_path))
    memory.apply_memory_updates([{"op": "add", "text": "带孩子旅行时安静酒店"}])
    result = await PreferenceAnalysisAgent(model).analyze({"history": samples(3), "memories": []})
    save_preference_analysis(memory, result, "bootstrap")
    assert [
        m["source"] for m in [m for m in memory.data["travel_memories"] if m["status"] == "active"]
    ] == ["explicit", "history"]
    assert (
        "尚不确定"
        in [m for m in memory.data["travel_memories"] if m["status"] == "active"][1]["text"]
    )

    async def bad(messages):
        return SimpleNamespace(
            text='{"memories":[{"text":"高铁","evidence_trip_ids":["invented"]}],"covered_trip_ids":["invented"]}'
        )

    with pytest.raises(ValueError):
        await PreferenceAnalysisAgent(bad).analyze({"history": samples(3)})


@pytest.mark.asyncio
async def test_real_request_bootstrap_once_after_three_plans_and_reopen(tmp_path):
    from copy import deepcopy

    from support.native import ScriptedModel
    from support.workflow import populated_workflow

    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    ids = ["confirmed-trip-" + str(i) for i in range(3)]
    model = ScriptedModel(
        [json.dumps({"memories": [], "covered_trip_ids": ids}), "已使用上下文中的长期偏好。"]
    )
    app = create_application(
        Settings(_env_file=None),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="planning",
    )
    for identity in ids:
        work = deepcopy(populated_workflow(quoted=True))
        work.update(id=identity, user_confirmed=True)
        work["status"] = "completed"
        work["validation"] = {"valid": True}
        app.trips.save(work, expected_revision=None)
    result = await app.run("根据长期记忆准备新的行程")
    assert result["status"] == "completed" and len(model.messages) == 2
    assert app.preferences.data["preference_analysis"]["initial_summary_done"]
    records = [
        r
        for r in app.sessions.read_runs()
        if r["type"] == "model_call" and r["stage"] == "agent:preference_analysis"
    ]
    assert len(records) == 1 and records[0]["turn_id"]
    model.script = ["已使用上下文中的长期偏好。"]
    reopened = create_application(
        Settings(_env_file=None),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="later",
    )
    await reopened.run("继续准备行程")
    assert len(model.messages) == 3
    assert set(reopened.preferences.data["preference_analysis"]["covered_trip_ids"]) == set(ids)
