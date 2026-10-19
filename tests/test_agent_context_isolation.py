"""Native context and legacy history adapters keep child audit outside model requests."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from support.native import record_user

from trip_assistant.memory.compaction import prepare_context
from trip_assistant.memory.context import bounded, project_events
from trip_assistant.storage.sessions import SessionStore


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


@pytest.mark.asyncio
async def test_main_request_rebuilds_from_compacted_complete_old_history(tmp_path):
    from trip_assistant.agents.main import MainAgent

    sessions = SessionStore(tmp_path, "alice", "s")
    for i in range(6):
        turn = record_user(sessions, "old" + str(i) + "x" * 150)
        sessions.append(
            {
                "type": "message",
                "scope": "session",
                "role": "assistant",
                "turn_id": turn,
                "content": "a" * 150,
                "final": True,
            }
        )
    record_user(sessions, "本轮简短请求")
    inputs = []

    async def model(messages, **kwargs):
        inputs.append(deepcopy(messages))
        return SimpleNamespace(
            text="已讨论完整旧需求" if "概括完整旧用户轮次" in messages[0]["content"] else "ok"
        )

    messages = await prepare_context(
        sessions,
        prefix=[{"role": "system", "content": "主指令"}],
        tools=[],
        model=model,
        budget_tokens=2200,
        counter=lambda payload: len(json.dumps(payload, ensure_ascii=False)),
    )
    result = await MainAgent(model).respond(messages, tools=[])
    assert result.text == "ok" and 2 <= len(inputs) <= 4
    assert "已讨论完整旧需求" in str(inputs[-1])
    assert inputs[-1][-1]["content"] == "本轮简短请求"
    assert not any(r["content"].startswith("old0") for r in inputs[-1])
    assert sessions.read_state()["covered_through_seq"] % 2 == 0


def test_child_projection_preserves_candidate_ids_and_sources_without_mutation():
    original = sample_result()
    saved = deepcopy(original)
    view = bounded(original)
    assert "PRIVATE_TOOL_MARKER" not in json.dumps(view)
    assert view["domain_results"]["train"]["items"][0]["id"] == "c1"
    assert (
        view["domain_results"]["train"]["source"] == original["domain_results"]["train"]["source"]
    )
    assert original == saved


@pytest.mark.asyncio
async def test_child_raw_tool_marker_absent_from_main_requests_and_summary(tmp_path):
    sessions = SessionStore(tmp_path, "alice", "s")
    for _ in range(3):
        turn = record_user(sessions, "查询" * 100)
        sessions.append(
            {"type": "agent_stage", "scope": "policy", "turn_id": turn, "content": sample_result()}
        )
        sessions.append(
            {
                "type": "message",
                "scope": "session",
                "role": "assistant",
                "turn_id": turn,
                "content": json.dumps({"final_answer": "已查票", "results": sample_result()}),
                "metadata": {"format": "main_result"},
                "final": True,
            }
        )
    record_user(sessions, "继续")
    inputs = []

    async def model(messages):
        inputs.extend(deepcopy(messages))
        return SimpleNamespace(text="已查询过车票")

    messages = await prepare_context(
        sessions,
        prefix=[],
        tools=[],
        model=model,
        budget_tokens=950,
        counter=lambda payload: len(json.dumps(payload, ensure_ascii=False)),
    )
    assert inputs and "PRIVATE_TOOL_MARKER" not in str(inputs)
    assert "PRIVATE_TOOL_MARKER" not in str(messages)
    assert "PRIVATE_TOOL_MARKER" in str(sessions.read_events())
    assert project_events(sessions.read_events())[-1]["content"] == "继续"
