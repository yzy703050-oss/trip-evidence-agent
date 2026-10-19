"""Offline regression scenarios grouped by responsibility."""

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from trip_assistant.runtime.telemetry import MeteredModel, model_stage
from trip_assistant.settings import Settings
from trip_assistant.storage.identity import resolve_user_directory
from trip_assistant.storage.preferences import LongTermMemory
from trip_assistant.storage.sessions import SessionStore

# v0_telemetry


@pytest.mark.asyncio
async def test_new_query_error_does_not_inherit_previous_result_metadata(tmp_path):
    from support.cli_session_resume import app_for

    app = app_for(tmp_path)
    app.last_result = {"stop_reason": "old_failure", "finalization_method": "old_renderer"}

    def fail(messages):
        raise OSError("model request failed")

    app.raw_model.script = [fail]
    await app.process_query("普通请求")
    row = [r for r in app.app.sessions.read_runs() if r["type"] == "query_run"][-1]
    assert row["stop_reason"] == "model_error"
    assert app.last_result["finalization_method"] != "old_renderer"
    assert "old_failure" not in str(row)


@pytest.mark.asyncio
async def test_metered_model_records_usage_latency_and_configured_cost(tmp_path):
    class Model:
        model_name = "test-model"

        async def __call__(self, _messages):
            return SimpleNamespace(
                content="ok", usage=SimpleNamespace(input_tokens=100, output_tokens=50)
            )

    store = SessionStore(tmp_path, "alice", "s1")
    model = MeteredModel(
        Model(), store.append_run, input_usd_per_million=2.0, output_usd_per_million=4.0
    )
    with model_stage("intent"):
        response = await model([{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    record = store.read_runs()[0]
    assert record["stage"] == "intent"
    assert record["model"] == "test-model"
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["estimated_cost_usd"] == pytest.approx(0.0004)
    assert record["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_missing_usage_or_rate_is_recorded_as_unknown_not_zero(tmp_path):
    class Model:
        async def __call__(self, _messages):
            return SimpleNamespace(content="ok", usage=None)

    store = SessionStore(tmp_path, "alice", "s1")
    await MeteredModel(Model(), store.append_run)([{"role": "user", "content": "hi"}])
    record = store.read_runs()[0]
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["estimated_cost_usd"] is None


def test_optional_model_prices_load_from_local_dotenv(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_INPUT_USD_PER_1M_TOKENS=0.27\nLLM_OUTPUT_USD_PER_1M_TOKENS=1.1\n", encoding="utf-8"
    )
    settings = Settings(_env_file=env_path)
    assert settings.llm_input_usd_per_1m_tokens == 0.27
    assert settings.llm_output_usd_per_1m_tokens == 1.1


# v0_session_store


def test_interrupted_legacy_tail_is_preserved_and_next_database_event_is_readable(tmp_path):
    directory = resolve_user_directory(tmp_path, "alice") / "sessions/s1"
    directory.mkdir(parents=True)
    source = directory / "events.jsonl"
    row = {
        "user_id": "alice",
        "session_id": "s1",
        "seq": 1,
        "turn_id": "t1",
        "type": "message",
        "scope": "session",
        "role": "user",
        "content": "完整",
    }
    original = (json.dumps(row) + "\n").encode("utf-8") + b'{"turn_id":"interrupted"'
    source.write_bytes(original)
    store = SessionStore(tmp_path, "alice", "s1")
    assert [event["seq"] for event in store.read_events()] == [1]
    saved = store.append(
        {"turn_id": "t2", "type": "message", "scope": "session", "role": "user", "content": "继续"}
    )
    assert saved["seq"] == 2
    assert [event["seq"] for event in store.read_events()] == [1, 2]
    assert source.read_bytes() == original


def test_session_events_survive_restart_in_append_order(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    first = store.append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "去北京",
        }
    )
    second = store.append(
        {
            "turn_id": "t1",
            "scope": "agent:planner",
            "type": "tool_call",
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "name": "search", "arguments": {}}],
        }
    )
    third = store.append(
        {
            "turn_id": "t1",
            "scope": "agent:planner",
            "type": "tool_result",
            "tool_call_id": "call-1",
            "content": {"price": 500},
            "status": "success",
        }
    )

    assert [first["seq"], second["seq"], third["seq"]] == [1, 2, 3]
    restored = SessionStore(tmp_path, "alice", "s1").read_events()
    assert [event["type"] for event in restored] == ["message", "tool_call", "tool_result"]
    assert restored[2]["tool_call_id"] == restored[1]["tool_calls"][0]["id"]
    assert SessionStore(tmp_path, "alice", "other").read_events() == []


def test_parallel_appends_receive_unique_monotonic_sequence(tmp_path):
    store = SessionStore(tmp_path, "alice", "s1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: store.append(
                    {
                        "turn_id": "t1",
                        "scope": "agent:a",
                        "type": "message",
                        "role": "assistant",
                        "content": str(index),
                    }
                ),
                range(40),
            )
        )

    assert [event["seq"] for event in store.read_events()] == list(range(1, 41))


def test_path_components_cannot_escape_storage_root(tmp_path):
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "..", "s1")
    with pytest.raises(ValueError):
        SessionStore(tmp_path, "alice", "../other")


def test_legacy_user_json_migrates_once_without_deleting_source(tmp_path):
    legacy = {
        "user_id": "alice",
        "preferences": [{"type": "hotel_brands", "value": ["全季"]}],
        "trip_history": [{"trip_id": "trip_1", "destination": "北京"}],
        "chat_history": [
            {
                "role": "user",
                "content": "去北京",
                "timestamp": "2026-01-01T10:00:00",
                "session_id": "s1",
            },
            {
                "role": "assistant",
                "content": "好的",
                "timestamp": "2026-01-01T10:00:01",
                "session_id": "s1",
            },
            {
                "role": "user",
                "content": "去上海",
                "timestamp": "2026-01-02T10:00:00",
                "session_id": "s2",
            },
        ],
        "statistics": {
            "total_messages": 3,
            "total_trips": 1,
            "total_queries": 2,
            "frequent_destinations": {"北京": 1},
        },
    }
    old_path = tmp_path / "alice.json"
    old_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    memory = LongTermMemory("alice", str(tmp_path))
    assert memory.get_preference("hotel_brands") == ["全季"]
    assert memory.get_trip_history()[0]["destination"] == "北京"
    assert [item["content"] for item in memory.get_chat_history(session_id="s1")] == [
        "去北京",
        "好的",
    ]
    assert memory.db_path.exists()
    assert memory.data["statistics"]["total_queries"] == 2
    assert not (resolve_user_directory(tmp_path, "alice") / "trips.json").exists()
    with memory.storage.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM travel_history").fetchone()[0] == 1
    assert len(SessionStore(tmp_path, "alice", "s1").read_events()) == 2
    assert json.loads(old_path.read_text(encoding="utf-8")) == legacy

    reopened = LongTermMemory("alice", str(tmp_path))
    assert len(reopened.get_chat_history(session_id="s1")) == 2
    assert len(reopened.get_chat_history(session_id="s2")) == 1
