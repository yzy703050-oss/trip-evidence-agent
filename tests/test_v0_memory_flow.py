"""V0 entry points record the real user turn and direct agent execution."""

import io
import json

import pytest
from agents.runtime import Msg
from rich.console import Console

from agents.execution_harness import ExecutionHarness
from cli import TripEvidenceCLI
from context.long_term_memory import LongTermMemory
from context.memory_manager import MemoryManager
from context.session_store import SessionStore
from context.telemetry import MeteredModel


def test_manual_profile_value_is_visible_and_not_overwritten_by_agent(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.save_preference("hotel_brands", ["全季"])
    memory.set_user_preference("hotel_brands", ["汉庭"])
    memory.save_preference("hotel_brands", ["如家"])

    reopened = LongTermMemory("alice", str(tmp_path))
    assert reopened.get_preference("hotel_brands") == ["汉庭"]
    assert (
        reopened.data["preferences"][0]["source"]
        == "user"
    )
    reopened.delete_user_preference("hotel_brands")
    assert reopened.get_preference("hotel_brands") is None


def test_agent_append_helpers_respect_manual_profile_and_deletion(tmp_path):
    memory = LongTermMemory("alice", str(tmp_path))
    memory.set_user_preference("hotel_brands", ["汉庭"])
    memory.set_user_preference("airlines", ["国航"])
    memory.add_hotel_brand("如家")
    memory.add_airline("东航")
    assert memory.get_preference("hotel_brands") == ["汉庭"]
    assert memory.get_preference("airlines") == ["国航"]

    memory.delete_user_preference("hotel_brands")
    memory.add_hotel_brand("如家")
    assert memory.get_preference("hotel_brands") is None


def test_clear_history_removes_sessions_but_keeps_profile_after_restart(tmp_path):
    legacy = tmp_path / "alice.json"
    legacy.write_text(
        json.dumps(
            {
                "user_id": "alice",
                "preferences": [{"type": "hotel_brands", "value": ["汉庭"]}],
                "trip_history": [{"destination": "北京"}],
                "chat_history": [{"role": "user", "content": "旧消息", "session_id": "old"}],
            }
        ),
        encoding="utf-8",
    )
    memory = LongTermMemory("alice", str(tmp_path))
    memory.add_chat_message("user", "新消息", "new")
    assert len(memory.get_chat_history()) == 2

    memory.clear_history()
    reopened = LongTermMemory("alice", str(tmp_path))
    assert reopened.get_chat_history() == []
    assert reopened.get_trip_history() == []
    assert reopened.get_preference("hotel_brands") == ["汉庭"]
    assert legacy.exists()


@pytest.mark.asyncio
async def test_cli_records_user_before_main_and_one_final(tmp_path):
    memory = MemoryManager('alice', 's1', storage_path=str(tmp_path))
    app = TripEvidenceCLI()
    app.memory_manager = memory
    class Main:
        async def plan(self, context):
            assert [e['role'] for e in memory.session_store.read_events()] == ['user']
            assert context['original_query'] == 'hello'
            return {'response_mode': 'direct', 'agent_schedule': [], 'final_answer': 'hello'}
    app.harness = ExecutionHarness(main_agent=Main(), agent_registry={}, memory_manager=memory)
    assert await app.process_query('hello')
    events = memory.session_store.read_events()
    assert [e['role'] for e in events] == ['user', 'assistant']
    assert events[1]['final'] is True
    assert events[0]['turn_id'] == events[1]['turn_id']
    assert memory.session_store.read_runs()[-1]['status'] == 'completed'


@pytest.mark.asyncio
async def test_bad_main_returns_safe_error_and_records_real_user(tmp_path):
    memory = MemoryManager('alice', 's1', storage_path=str(tmp_path))
    app = TripEvidenceCLI()
    app.memory_manager = memory
    class Main:
        async def plan(self, context): return 'not json'
    app.harness = ExecutionHarness(main_agent=Main(), agent_registry={}, memory_manager=memory)
    assert await app.process_query('hello') is False
    events = memory.session_store.read_events()
    assert events[0]['role'] == 'user'
    assert json.loads(events[-1]['content'])['error_code'] == 'invalid_plan'




def test_cli_can_select_existing_session_and_edit_profile(tmp_path, monkeypatch):
    SessionStore(tmp_path, "alice", "s1").append(
        {
            "turn_id": "t1",
            "scope": "session",
            "type": "message",
            "role": "user",
            "content": "旧问题",
        }
    )
    cli = TripEvidenceCLI()
    cli.user_id = "alice"
    cli.console = Console(file=io.StringIO(), force_terminal=False)
    monkeypatch.setattr("cli.Prompt.ask", lambda *_args, **_kwargs: "s1")
    assert cli.choose_session_id(storage_path=str(tmp_path)) != "s1"

    cli.memory_manager = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    cli.handle_preferences_command('preferences set hotel_brands ["汉庭", "全季"]')
    assert cli.memory_manager.long_term.get_preference("hotel_brands") == ["汉庭", "全季"]
    cli.handle_preferences_command("preferences delete hotel_brands")
    assert cli.memory_manager.long_term.get_preference("hotel_brands") is None
