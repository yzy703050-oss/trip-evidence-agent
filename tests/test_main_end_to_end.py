import json
from types import SimpleNamespace
import pytest
from agents.main_agent import MainAgent
from agents.orchestration_agent import OrchestrationAgent
from agents.contracts import RunState
from context.memory_manager import MemoryManager
from context.telemetry import MeteredModel
from travel_data.tools import ToolExecutor
from test_information_agent_loop import info_class, Public


@pytest.mark.asyncio
@pytest.mark.parametrize('itinerary', [False, True])
async def test_real_runtime_measures_actual_main_and_info_calls(tmp_path, itinerary):
    memory = MemoryManager('alice', 'actual', storage_path=str(tmp_path))
    answers = [
        {'response_mode': 'itinerary' if itinerary else 'answer', 'finalization_mode': 'synthesize' if itinerary else 'forward',
         'agent_schedule': [{'agent_name': 'information_query', 'requested_domains': ['weather']}]},
        [{'type': 'tool_use', 'id': 'w', 'name': 'weather_query', 'input': {'city': '北京', 'date': '2026-10-10'}}],
        {'summary': '天气已查', 'travel_conditions': {'destination': '北京', 'start_date': '2026-10-10'}},
        {'action': 'itinerary', 'planning_complete': True, 'itinerary': {'daily_plans': [{'day': 1, 'date': '2026-10-10',
            'activities': [{'location_ref': 'suggestion:city_walk'}]}]}}
    ]
    async def raw(messages, **kwargs):
        value = answers.pop(0)
        return SimpleNamespace(content=value) if isinstance(value, list) else SimpleNamespace(text=json.dumps(value, ensure_ascii=False))
    model = MeteredModel(raw, memory.session_store.append_run)
    harness = OrchestrationAgent(main_agent=MainAgent(model),
        agent_registry={'information_query': info_class()(model=model, tool_executor=ToolExecutor({}, Public()), memory_manager=memory)}, memory_manager=memory)
    result = await harness.run_turn({'original_query': '规划北京行程' if itinerary else '北京明天天气'}, RunState(memory.start_turn('query')))
    calls = [r for r in memory.session_store.read_runs() if r['type'] == 'model_call']
    assert [r['stage'] for r in calls] == ['main:plan', 'agent:information_query', 'agent:information_query'] + (['main:itinerary'] if itinerary else [])
    assert result['finalization_method'] == ('synthesize' if itinerary else 'forward')
    assert len(memory.long_term.get_trip_history()) == int(itinerary)


@pytest.mark.asyncio
async def test_financial_forwarding_rebuilds_display_from_real_offers():
    import io
    from rich.console import Console
    from cli import TripEvidenceCLI
    from test_information_tools import Provider
    from test_main_harness import Main
    calls = 0
    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': 'h', 'name': 'hotel_search',
                'input': {'city': '北京', 'check_in': '2026-10-10', 'check_out': '2026-10-11', 'guests': 1}}])
        return SimpleNamespace(text='{"summary":"只需299999元，已预订","domain_results":{"hotel":{"items":[{"price":"299999"}]}}}')
    main = Main([{'agent_name': 'information_query', 'requested_domains': ['hotel']}])
    harness = OrchestrationAgent(main_agent=main, agent_registry={'information_query': info_class()(model=model,
        tool_executor=ToolExecutor({'hotel_search': Provider()}))})
    result = await harness.run_turn({'original_query': '查询北京酒店'}, RunState('a'))
    assert result['finalization_method'] == 'forward'
    assert main.finalize_calls == 0
    app = TripEvidenceCLI()
    output = io.StringIO()
    app.console = Console(file=output, width=240, color_system=None)
    app._display_results(result)
    assert '299999' not in output.getvalue()
    assert '100' in output.getvalue() and 'https://example.com' in output.getvalue()
