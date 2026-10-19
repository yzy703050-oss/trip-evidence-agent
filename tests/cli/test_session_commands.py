from io import StringIO

import pytest
from rich.console import Console
from support.native import ScriptedModel

from trip_assistant.settings import Settings


def make_cli(root):
    from trip_assistant.cli import TripEvidenceCLI

    return TripEvidenceCLI(
        settings=Settings(_env_file=None),
        storage_root=root,
        model=ScriptedModel([]),
        providers={},
        console=Console(file=StringIO(), force_terminal=False),
    )


@pytest.mark.asyncio
async def test_explicit_commands_do_not_call_model_and_clear_keeps_preferences(tmp_path):
    cli = make_cli(tmp_path)
    cli.user_id = "a"
    cli._bind_session("s")
    cli.app.preferences.set_user_preference("home_location", "上海")
    for command in ("help", "history", "preferences", "status", "clear"):
        assert await cli.handle_command(command)
    assert (
        not cli.raw_model.messages and cli.app.preferences.get_preference("home_location") == "上海"
    )


def test_session_switch_failure_keeps_old_application(tmp_path, monkeypatch):
    cli = make_cli(tmp_path)
    cli.user_id = "a"
    cli._bind_session("current")
    before = cli.app

    def fail(*args, **kwargs):
        raise OSError("failed")

    monkeypatch.setattr("trip_assistant.cli.create_application", fail)
    with pytest.raises(OSError):
        cli._bind_session("old")
    assert cli.app is before and cli.session_id == "current"


@pytest.mark.asyncio
async def test_resume_only_reads_and_rebinds_without_model(tmp_path, monkeypatch):
    cli = make_cli(tmp_path)
    cli.user_id = "a"
    cli._bind_session("old")
    cli.app.sessions.append(
        {
            "scope": "session",
            "type": "message",
            "turn_id": "old",
            "role": "user",
            "content": "上海差旅主题",
        }
    )
    cli._bind_session("current")
    monkeypatch.setattr("trip_assistant.cli.Prompt.ask", lambda *args, **kwargs: "1")
    assert cli.resume_session() and cli.session_id == "old" and not cli.raw_model.messages
    assert "上海差旅主题" in cli.console.file.getvalue()


def test_session_switch_shares_breaker_and_slots_but_preserves_record_owner(tmp_path):
    cli = make_cli(tmp_path)
    cli.user_id = "a"
    cli._bind_session("first")
    first = cli.app
    for _ in range(5):
        lease = first.scheduler.breakers.acquire("vendor:search")
        first.scheduler.breakers.failure("vendor:search", lease)
    cli._bind_session("second")
    assert cli.app.scheduler.breakers.status("vendor:search") == "open"
    assert cli.app.scheduler.global_slots is first.scheduler.global_slots
    first.scheduler.record({"type": "probe_owner", "owner": "first"})
    cli.app.scheduler.record({"type": "probe_owner", "owner": "second"})
    assert [r["owner"] for r in first.sessions.read_runs() if r["type"] == "probe_owner"] == [
        "first"
    ]
    assert [r["owner"] for r in cli.app.sessions.read_runs() if r["type"] == "probe_owner"] == [
        "second"
    ]
