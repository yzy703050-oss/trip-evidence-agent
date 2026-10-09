import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from agentscope.message import Msg
from rich.console import Console

import cli as cli_module
from config import Settings
from travel_data.juhe_train import JuheTrainProvider
from travel_data.providers import UnavailableProvider

KEY = 'offline-cli-test-key'
NOW = datetime(2026, 10, 9, 9, tzinfo=timezone(timedelta(hours=8)))


def initialize(monkeypatch, key):
    # No model request or persistence is needed to exercise the real startup wiring.
    monkeypatch.setattr(cli_module, 'get_settings', lambda: Settings(_env_file=None, juhe_train_api_key=key), raising=False)
    monkeypatch.setattr(cli_module, 'init_agentscope', lambda: None)
    monkeypatch.setattr(cli_module, 'OpenAIChatModel', lambda **kwargs: object())
    monkeypatch.setattr(cli_module, 'MemoryManager', lambda **kwargs: SimpleNamespace(session_store=SimpleNamespace(append_run=lambda record: None)))
    monkeypatch.setattr(cli_module, 'MeteredModel', lambda *args, **kwargs: object())
    monkeypatch.setattr(cli_module, 'MainAgent', lambda **kwargs: object())
    monkeypatch.setattr(cli_module, 'OrchestrationAgent', lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(cli_module.Prompt, 'ask', lambda *args, **kwargs: 'offline-user')
    monkeypatch.setattr(cli_module.TripEvidenceCLI, 'choose_session_id', lambda self: 'offline-session')
    app = cli_module.TripEvidenceCLI()
    output = io.StringIO()
    app.console = Console(file=output, width=240, color_system=None)
    asyncio.run(app.initialize_system())
    app.orchestrator.agent_registry.console = app.console
    return app, output


def test_local_env_key_is_loaded_and_hidden_in_repr(tmp_path, monkeypatch):
    monkeypatch.delenv('JUHE_TRAIN_API_KEY', raising=False)
    env = tmp_path / '.env'
    env.write_text('JUHE_TRAIN_API_KEY=' + KEY, encoding='utf-8')
    settings = Settings(_env_file=env)
    assert settings.juhe_train_api_key == KEY
    assert KEY not in repr(settings)
    assert Settings(_env_file=None).juhe_train_api_key == ''


def test_example_env_loads_with_optional_pricing_unset(monkeypatch):
    monkeypatch.delenv('LLM_INPUT_USD_PER_1M_TOKENS', raising=False)
    monkeypatch.delenv('LLM_OUTPUT_USD_PER_1M_TOKENS', raising=False)
    settings = Settings(_env_file='.env.example')
    assert settings.llm_input_usd_per_1m_tokens is None
    assert settings.llm_output_usd_per_1m_tokens is None


@pytest.mark.parametrize('key', ['', '   '])
def test_empty_key_keeps_train_unavailable(monkeypatch, key):
    app, output = initialize(monkeypatch, key)
    registry = app.orchestrator.agent_registry
    assert 'train_search' not in registry.providers
    agent = registry['information_query'].tool_executor
    from agents.contracts import RunState
    result = asyncio.run(agent.execute('train_search', {'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'}, RunState('a'), call_id='1'))
    assert result['status'] == 'unavailable'
    app._display_results({'domain_results': {'train': result}})
    assert 'unavailable' in output.getvalue()


def test_startup_injects_juhe_and_agent_cli_show_sourced_quote(monkeypatch, caplog):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {'error_code': 0, 'reason': '查询成功', 'result': [{
                'train_no': 'G25', 'departure_station': '北京南', 'arrival_station': '苏州北',
                'departure_time': '18:04', 'arrival_time': '22:32', 'enable_booking': 'Y',
                'prices': [{'seat_name': '二等座', 'seat_type_code': 'O', 'price': '627.50', 'num': '1'}]
            }]}
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()
    monkeypatch.setattr('travel_data.juhe_train.requests.post', post)
    app, output = initialize(monkeypatch, KEY)
    registry = app.orchestrator.agent_registry
    provider = registry.providers.get('train_search')
    assert isinstance(provider, JuheTrainProvider)
    provider._now_fn = lambda: NOW
    agent = registry['information_query'].tool_executor
    assert agent.providers['train_search'] is provider
    from agents.contracts import RunState
    result = asyncio.run(agent.execute('train_search', {'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'}, RunState('a'), call_id='1'))
    assert result['status'] == 'ok'
    assert len(calls) == 1
    assert calls[0][1]['data']['key'] == KEY
    app._display_results({'domain_results': {'train': result}})
    text = output.getvalue()
    for expected in ['G25', '二等座', '627.50', 'available', '聚合数据', 'https://www.juhe.cn/docs/api/id/817', '2026-10-09T09:00:00+08:00']:
        assert expected in text
    assert KEY not in text + caplog.text + json.dumps(result)

@pytest.mark.asyncio
async def test_cli_paid_train_is_not_replayed_after_stage_write_failure(tmp_path, monkeypatch):
    from agents.contracts import RunState
    from agents.orchestration_agent import OrchestrationAgent
    from context.memory_manager import MemoryManager
    from travel_data.tools import ToolExecutor
    from test_main_harness import Main
    calls = []
    class Response:
        status_code = 200
        def json(self): return {'error_code': 0, 'result': []}
    def post(url, **kwargs): calls.append(url); return Response()
    executor = ToolExecutor({'train_search': JuheTrainProvider(KEY, http_post=post, now_fn=lambda: NOW)})
    class Info:
        async def run(self, context, run):
            await executor.execute('train_search', {'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'}, run, call_id='1')
            return {'status': 'partial', 'domain_results': run.domain_results}
    memory = MemoryManager('alice', 'failure', storage_path=str(tmp_path))
    app = cli_module.TripEvidenceCLI()
    app.memory_manager = memory
    app.orchestrator = OrchestrationAgent(main_agent=Main(), agent_registry={'information_query': Info()}, memory_manager=memory)
    def fail(*args, **kwargs): raise OSError('stage write failed')
    monkeypatch.setattr(memory, 'record_agent_stage', fail)
    with pytest.raises(OSError): await app.process_query('query')
    assert len(calls) == 1
    assert KEY not in json.dumps(memory.session_store.read_runs())
