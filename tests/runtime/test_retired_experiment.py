from copy import deepcopy

import pytest
from pydantic import ValidationError
from support.native import NativeRuntimeFixture

from trip_assistant.runtime.recovery import restore_turn
from trip_assistant.settings import Settings
from trip_assistant.storage.sessions import SessionStore


def test_retired_true_configuration_is_rejected():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, single_pass_planning=True)
    assert Settings(_env_file=None, single_pass_planning=False)


def test_disabled_legacy_environment_still_loads_but_true_is_rejected(monkeypatch):
    monkeypatch.setenv("SINGLE_PASS_PLANNING", "false")
    assert Settings(_env_file=None).single_pass_planning is False
    monkeypatch.setenv("SINGLE_PASS_PLANNING", "true")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_old_experiment_restore_preserves_events_and_closes_pending_without_replay(tmp_path):
    store = SessionStore(tmp_path, "a", "s")
    store.record_call({"id": "q", "name": "query", "arguments": {}}, "old")
    store.checkpoint_turn("old", {"single_pass_planning": True, "query": "旧实验"})
    events = deepcopy(store.read_events())
    restored = restore_turn(store, "old", settings=Settings(_env_file=None))
    assert restored["can_resume"] is False
    assert "退役" in restored["reason"]
    assert store.read_events()[:len(events)] == events
    result = store.read_events(turn_id="old", event_type="tool_result")[-1]["content"]
    assert result["status"] == "unknown"
    assert result["error"]["code"] == "RETIRED_EXPERIMENT"
    assert store.pending_tool_calls() == []
    assert restored["unknown_calls"] == ["q"]


async def test_explicit_retired_context_is_rejected_before_model_or_events(tmp_path):
    runtime = NativeRuntimeFixture(tmp_path)
    before = deepcopy(runtime.sessions.read_events())
    with pytest.raises(ValueError, match="退役"):
        await runtime.run("继续", context={"single_pass_planning": True})
    assert runtime.model.messages == []
    assert runtime.sessions.read_events() == before


async def test_new_request_after_retired_pending_turn_uses_current_path(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application

    model = ScriptedModel(["你好"])
    app = create_application(
        Settings(_env_file=None), model=model, providers={}, storage_root=tmp_path
    )
    app.sessions.record_call({"id": "old-q", "name": "query", "arguments": {}}, "old")
    app.sessions.checkpoint_turn("old", {"single_pass_planning": True, "query": "旧实验"})
    result = await app.run("你好")
    assert result["final_answer"] == "你好"
    assert len(model.messages) == 1
    old = app.sessions.read_events(turn_id="old", event_type="tool_result")
    assert len(old) == 1 and old[0]["content"]["status"] == "unknown"
