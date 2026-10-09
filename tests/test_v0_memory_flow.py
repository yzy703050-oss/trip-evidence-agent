"""V0 entry points record the real user turn and direct agent execution."""

import io
import json

import pytest
from agentscope.message import Msg
from rich.console import Console

from agents.orchestration_agent import OrchestrationAgent
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
        json.loads(reopened.profile_path.read_text(encoding="utf-8"))["preferences"][0]["source"]
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
async def test_cli_records_user_before_intent_and_final_only_after_dispatch(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    cli = TripEvidenceCLI()
    cli.memory_manager = memory
    cli.console = Console(file=io.StringIO(), force_terminal=False)

    async def no_history(_query):
        return ""

    cli._get_long_term_summary = no_history

    class Intent:
        async def reply(self, incoming):
            assert [event["role"] for event in memory.session_store.read_events()] == ["user"]
            assert [msg.content for msg in incoming if msg.role == "user"] == ["去北京"]
            return Msg(name="intent", role="assistant", content=json.dumps({"agent_schedule": []}))

    class Dispatch:
        async def reply(self, _incoming):
            assert [event["role"] for event in memory.session_store.read_events()] == ["user"]
            return Msg(
                name="dispatch",
                role="assistant",
                content=json.dumps({"status": "completed", "results": []}),
            )

    cli.intention_agent = Intent()
    cli.orchestrator = Dispatch()
    await cli.process_query("去北京")

    events = memory.session_store.read_events()
    assert [event["role"] for event in events] == ["user", "assistant"]
    assert events[0]["turn_id"] == events[1]["turn_id"]
    assert events[1]["final"] is True
    assert all(event["type"] == "message" for event in events)
    assert memory.session_store.read_runs()[-1]["type"] == "query_run"
    assert memory.session_store.read_runs()[-1]["status"] == "completed"
    assert any(
        run["type"] == "agent_plan" and run["agents"] == []
        for run in memory.session_store.read_runs()
    )


@pytest.mark.asyncio
async def test_bad_intent_keeps_user_record_without_fabricating_answer(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    cli = TripEvidenceCLI()
    cli.memory_manager = memory
    cli.console = Console(file=io.StringIO(), force_terminal=False)

    async def no_history(_query):
        return ""

    cli._get_long_term_summary = no_history

    class Intent:
        async def reply(self, _incoming):
            return Msg(name="intent", role="assistant", content="not json")

    cli.intention_agent = Intent()
    await cli.process_query("去北京")

    assert [event["role"] for event in memory.session_store.read_events()] == ["user"]
    assert memory.session_store.read_runs()[-1]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_bad_orchestration_result_does_not_record_fabricated_final_answer(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    cli = TripEvidenceCLI()
    cli.memory_manager = memory
    cli.console = Console(file=io.StringIO(), force_terminal=False)

    async def no_history(_query):
        return ""

    cli._get_long_term_summary = no_history

    class Intent:
        async def reply(self, _incoming):
            return Msg(name="intent", role="assistant", content=json.dumps({"agent_schedule": []}))

    class Dispatch:
        async def reply(self, _incoming):
            return Msg(name="dispatch", role="assistant", content="not json")

    cli.intention_agent = Intent()
    cli.orchestrator = Dispatch()
    assert await cli.process_query("去北京") is False
    assert [event["role"] for event in memory.session_store.read_events()] == ["user"]
    assert memory.session_store.read_runs()[-1]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_direct_child_agent_call_is_stage_event_not_tool_call(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    memory.start_turn("查询酒店")

    class RawModel:
        async def __call__(self, _messages):
            from types import SimpleNamespace

            return SimpleNamespace(content="ok", usage=None)

    metered = MeteredModel(RawModel(), memory.session_store.append_run)

    class Child:
        async def reply(self, _message):
            await metered([{"role": "user", "content": "查酒店"}])
            return Msg(name="child", role="assistant", content=json.dumps({"answer": "找到酒店"}))

    orchestrator = OrchestrationAgent(
        agent_registry={"information_query": Child()}, memory_manager=memory
    )
    intent = Msg(
        name="intent",
        role="assistant",
        content=json.dumps(
            {
                "agent_schedule": [
                    {
                        "agent_name": "information_query",
                        "priority": 1,
                        "reason": "查询",
                        "expected_output": "酒店",
                    }
                ]
            }
        ),
    )
    await orchestrator.reply(intent)

    events = memory.session_store.read_events()
    assert any(
        event["type"] == "stage_complete" and event["agent_name"] == "information_query"
        for event in events
    )
    assert not any(event["type"] == "tool_call" for event in events)
    assert memory.session_store.read_runs()[0]["stage"] == "agent:information_query"


@pytest.mark.asyncio
async def test_child_receives_compacted_summary_without_replaying_covered_dialogue(tmp_path):
    memory = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    for index in range(2):
        turn = memory.start_turn(f"旧问题{index}" + "很长" * 12)
        memory.record_message("assistant", "旧回答" + "很长" * 12, turn, final=True)
    memory.start_turn("新问题")
    memory.compact_if_needed(
        10,
        summarizer=lambda _old, _selected: "旧需求摘要",
        keep_recent_turns=1,
        token_counter=lambda event: len(str(event.get("content", ""))),
    )
    observed = []

    class Child:
        async def reply(self, incoming):
            observed.append(json.loads(incoming.content)["context"])
            return Msg(name="child", role="assistant", content=json.dumps({"answer": "好的"}))

    orchestrator = OrchestrationAgent(
        agent_registry={"information_query": Child()}, memory_manager=memory
    )
    intent = Msg(
        name="intent",
        role="assistant",
        content=json.dumps(
            {"agent_schedule": [{"agent_name": "information_query", "priority": 1}]}
        ),
    )
    await orchestrator.reply(intent)
    assert observed[0]["session_summary"] == "旧需求摘要"
    assert [item["content"] for item in observed[0]["recent_dialogue"]] == ["新问题"]


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
    assert cli.choose_session_id(storage_path=str(tmp_path)) == "s1"

    cli.memory_manager = MemoryManager("alice", "s1", storage_path=str(tmp_path))
    cli.handle_preferences_command('preferences set hotel_brands ["汉庭", "全季"]')
    assert cli.memory_manager.long_term.get_preference("hotel_brands") == ["汉庭", "全季"]
    cli.handle_preferences_command("preferences delete hotel_brands")
    assert cli.memory_manager.long_term.get_preference("hotel_brands") is None
