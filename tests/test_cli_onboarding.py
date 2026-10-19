"""Residence is an explicit user fact, required once per local CLI identity."""

import io

import pytest
from rich.console import Console
from support.native import make_application

from trip_assistant.cli import TripEvidenceCLI
from trip_assistant.storage.preferences import LongTermMemory


def app_for(tmp_path, user="alice", session="s1"):
    app = TripEvidenceCLI()
    app.app = make_application(user, session, storage_path=str(tmp_path))
    app.console = Console(file=io.StringIO(), width=240, color_system=None)
    return app


def test_residence_is_required_and_saved_as_user_fact(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    replies = iter(["", "   ", " 上海 "])
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *_a, **_kw: next(replies))
    assert app.ensure_home_location() == "上海"
    assert "必须填写" in app.console.file.getvalue()
    reopened = LongTermMemory("alice", str(tmp_path))
    assert reopened.get_preference("home_location") == "上海"
    profile = reopened.data
    assert {"type": "home_location", "value": "上海", "source": "user"} in profile["preferences"]


def test_existing_residence_is_not_asked_again_across_sessions(tmp_path, monkeypatch):
    LongTermMemory("alice", str(tmp_path)).set_user_preference("home_location", "上海")
    app = app_for(tmp_path, session="s2")

    def unexpected_prompt(*_a, **_kw):
        pytest.fail("An existing residence must not require registration again")

    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", unexpected_prompt)
    assert app.ensure_home_location() == "上海"


def test_legacy_user_without_residence_is_completed_and_other_users_are_isolated(
    tmp_path, monkeypatch
):
    LongTermMemory("alice", str(tmp_path)).set_user_preference("hotel_brands", ["全季"])
    app = app_for(tmp_path)
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *_a, **_kw: "重庆")
    app.ensure_home_location()
    assert app.app.preferences.get_preference("hotel_brands") == ["全季"]
    assert LongTermMemory("bob", str(tmp_path)).get_preference("home_location") is None


def test_user_can_change_residence_and_agent_cannot_override_it(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *_a, **_kw: "上海")
    app.ensure_home_location()
    app.handle_preferences_command("preferences set home_location 杭州")
    assert app.app.preferences.save_preference("home_location", "北京") is False
    assert LongTermMemory("alice", str(tmp_path)).get_preference("home_location") == "杭州"


@pytest.mark.asyncio
async def test_real_startup_collects_residence_before_ready(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from support.native import ScriptedModel

    replies = iter(["new_user", "", "天津"])
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *_a, **_kw: next(replies))
    app = TripEvidenceCLI(model=ScriptedModel([]), providers={})
    app.console = Console(file=io.StringIO(), width=240, color_system=None)
    await app.initialize_system()
    assert app.app.preferences.get_preference("home_location") == "天津"
    assert "已开始新对话" in app.console.file.getvalue()
    assert LongTermMemory("new_user", "data/memory").get_preference("home_location") == "天津"


@pytest.mark.asyncio
async def test_residence_context_is_background_and_temporary_origin_does_not_overwrite(
    tmp_path, monkeypatch
):
    app = app_for(tmp_path)
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *_a, **_kw: "上海")
    app.ensure_home_location()
    app.app.raw_model.script = ["这次从北京出发。"]
    await app.process_query("我这次从北京出发")
    request = app.app.raw_model.messages[-1][0]
    assert "上海" in str(request) and "我这次从北京出发" in str(request)
    assert LongTermMemory("alice", str(tmp_path)).get_preference("home_location") == "上海"
