import io
import json
import pytest
from rich.console import Console
from cli import TripEvidenceCLI
from agents.execution_harness import ExecutionHarness
from context.memory_manager import MemoryManager
from test_main_harness import Main, Info


@pytest.mark.asyncio
async def test_forward_records_one_final_message(tmp_path):
    app = TripEvidenceCLI()
    app.memory_manager = MemoryManager('alice', 'forward', storage_path=str(tmp_path))
    app.harness = ExecutionHarness(main_agent=Main(), agent_registry={'information_query': Info()}, memory_manager=app.memory_manager)
    output = io.StringIO()
    app.console = Console(file=output, width=200, color_system=None)
    assert await app.process_query('北京明天天气') is True
    events = app.memory_manager.session_store.read_events()
    finals = [e for e in events if e.get('type') == 'message' and e.get('role') == 'assistant' and e.get('final')]
    assert len(finals) == 1
    assert json.loads(finals[0]['content'])['finalization_method'] == 'forward'
    assert output.getvalue().count('18～25') == 1
    assert 'https://example.com/weather' in output.getvalue()
    assert not any(e.get('agent_name') == 'main_finalize' for e in events)


@pytest.mark.asyncio
async def test_paid_query_not_replayed_after_record_failure(tmp_path, monkeypatch):
    app = TripEvidenceCLI()
    app.memory_manager = MemoryManager('alice', 'failure', storage_path=str(tmp_path))
    info = Info()
    app.harness = ExecutionHarness(main_agent=Main(), agent_registry={'information_query': info}, memory_manager=app.memory_manager)
    def fail(*args, **kwargs): raise OSError('record failed')
    monkeypatch.setattr(app.memory_manager, 'record_agent_stage', fail)
    with pytest.raises(OSError): await app.process_query('天气')
    assert info.executions == 1


@pytest.mark.asyncio
async def test_completed_itinerary_uses_new_conditions(tmp_path):
    memory = MemoryManager('alice', 'trip', storage_path=str(tmp_path))
    from agents.contracts import RunState
    class Planner(Main):
        async def finalize(self, context):
            return {'action': 'itinerary', 'planning_complete': True,
                    'itinerary': {'daily_plans': [{'day': 1, 'date': '2026-10-10'}]}}
    run = RunState(memory.start_turn('规划'), travel_conditions={'destination': '北京', 'start_date': '2026-10-10', 'purpose': '出差'})
    await ExecutionHarness(main_agent=Planner(mode='synthesize', response_mode='itinerary'),
        agent_registry={'information_query': Info()}, memory_manager=memory).run_turn({'original_query': '规划'}, run)
    assert memory.long_term.get_trip_history()[0]['destination'] == '北京'
    assert memory.long_term.get_trip_history()[0]['start_date'] == '2026-10-10'
    assert len([e for e in memory.session_store.read_events() if e.get('agent_name') == 'main_finalize']) == 1


@pytest.mark.asyncio
async def test_preference_persisted_once_despite_feedback(tmp_path, monkeypatch):
    from agentscope.message import Msg
    from agents.contracts import RunState
    from test_main_feedback import FeedbackMain
    memory = MemoryManager('alice', 'pref', storage_path=str(tmp_path))
    class Preference:
        async def reply(self, msg):
            return Msg('p', '{"preferences":[{"type":"airlines","value":"A","action":"append"}]}', 'assistant')
    main = FeedbackMain([{'agent_name': 'preference'}, {'agent_name': 'information_query', 'requested_domains': ['weather']}], mode='synthesize')
    calls = []
    save = memory.long_term.save_preference
    def record(*args, **kwargs): calls.append(args); return save(*args, **kwargs)
    monkeypatch.setattr(memory.long_term, 'save_preference', record)
    await ExecutionHarness(main_agent=main, agent_registry={'preference': Preference(), 'information_query': Info()}, memory_manager=memory).run_turn(
        {'original_query': 'pref'}, RunState(memory.start_turn('pref')))
    assert calls == [('airlines', ['A'])]


@pytest.mark.asyncio
async def test_standalone_plan_uses_main_harness(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('standalone', Path('.claude/skills/plan-trip/script/plan_trip_execution.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = TripEvidenceCLI()
    app.memory_manager = MemoryManager('alice', 'standalone', storage_path=str(tmp_path))
    app.harness = ExecutionHarness(main_agent=Main(), agent_registry={'information_query': Info()})
    result = await module.plan_trip('天气', app=app)
    assert result['finalization_method'] == 'forward'
