import json
from types import SimpleNamespace

import pytest


def samples(n):
    return [
        {
            "id": f"t{i}",
            "trip_plan_id": f"t{i}",
            "status": "completed",
            "analysis_eligible": True,
            "selection_source": "user",
            "user_confirmed": True,
        }
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "count,covered,expected", [(0, 0, 0), (2, 0, 0), (3, 0, 3), (5, 3, 0), (6, 3, 3)]
)
def test_incremental_groups_of_three(count, covered, expected):
    from trip_assistant.agents.preference import pending_analysis

    assert len(pending_analysis(samples(count), {f"t{i}" for i in range(covered)})) == expected


def test_unconfirmed_or_policy_or_duplicate_samples_do_not_trigger():
    from trip_assistant.agents.preference import pending_analysis

    data = samples(2) * 2 + [
        {
            "id": "bad",
            "status": "completed",
            "analysis_eligible": True,
            "selection_source": "assistant_proposal",
        }
    ]
    assert pending_analysis(data, set()) == []


@pytest.mark.asyncio
async def test_analysis_combines_existing_summary_and_atomically_unions_coverage(tmp_path):
    from trip_assistant.agents.preference import PreferenceAnalysisAgent, save_preference_analysis
    from trip_assistant.storage.preferences import LongTermMemory

    m = LongTermMemory("a", str(tmp_path))
    messages = []

    async def model(msg):
        messages.extend(msg)
        return SimpleNamespace(
            text=json.dumps(
                {
                    "memories": [{"text": "出差优先高铁，住宿安静", "evidence_trip_ids": ["t3"]}],
                    "covered_trip_ids": ["t3", "t4", "t5"],
                }
            )
        )

    save_preference_analysis(
        m,
        {
            "memories": [{"text": "出差优先高铁", "evidence_trip_ids": ["t0"]}],
            "covered_trip_ids": ["t0", "t1", "t2"],
        },
        "bootstrap",
    )
    result = await PreferenceAnalysisAgent(model).analyze(
        {
            "history": samples(6)[3:],
            "memories": m.data["travel_memories"],
            "existing_summary": "出差优先高铁",
        }
    )
    save_preference_analysis(m, result, "incremental")
    assert set(m.data["preference_analysis"]["covered_trip_ids"]) == {f"t{i}" for i in range(6)}
    assert "出差优先高铁" in messages[0]["content"]


def test_stale_analysis_after_manual_retraction_cannot_restore_or_advance(tmp_path):
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.storage.sqlite import StateConflictError

    m = LongTermMemory("a", str(tmp_path))
    m.apply_memory_updates([{"op": "add", "text": "出差高铁", "source": "history"}])
    revision = m.data["_revision"]
    identity = m.data["travel_memories"][0]["id"]
    m.apply_memory_updates([{"op": "retract", "id": identity}])
    with pytest.raises(StateConflictError):
        m.save_analysis(
            {
                "memories": [{"text": "出差高铁", "evidence_trip_ids": ["t0"]}],
                "covered_trip_ids": ["t0", "t1", "t2"],
            },
            expected_revision=revision,
        )
    assert not m.data.get("preference_analysis")
    assert m.data["travel_memories"][0]["status"] == "retracted"


@pytest.mark.asyncio
async def test_explicit_reanalysis_is_tool_operation_not_ordinary_write(tmp_path):
    from trip_assistant.storage.preferences import LongTermMemory
    from trip_assistant.tools.memory_write import build_memory_writer
    from trip_assistant.tools.registry import build_registry

    m = LongTermMemory("a", str(tmp_path))
    calls = []

    async def reanalyze(turn):
        calls.append(turn)
        return {"covered_trip_ids": ["t0", "t1", "t2"]}

    writer = build_memory_writer(m, reanalyze=reanalyze)
    tool = build_registry({"write_memory": writer})["write_memory"]
    args = tool.validate({"request_kind": "lookup", "operation": "reanalyze"})
    result = await writer(args, {"original_query": "重新分析历史出差偏好"})
    assert result["status"] == "ok" and len(calls) == 1
    await writer({"updates": [{"op": "add", "text": "出差尽量高铁"}]}, {})
    assert len(calls) == 1
