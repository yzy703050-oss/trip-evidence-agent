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
    monkeypatch.setattr(cli_module, 'IntentionAgent', lambda **kwargs: object())
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
    assert registry.providers == {}
    agent = registry['train_search']
    assert isinstance(agent.provider, UnavailableProvider)
    result = json.loads(asyncio.run(agent.reply(Msg('user', json.dumps({'context': {
        'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'
    }}), 'user'))).content)
    assert result['status'] == 'unavailable'
    app._display_results({'results': [{'agent_name': 'train_search', 'data': result}]})
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
    agent = registry['train_search']
    assert agent.provider is provider
    assert isinstance(registry['hotel_search'].provider, UnavailableProvider)
    assert isinstance(registry['travel_guide'].provider, UnavailableProvider)
    result = json.loads(asyncio.run(agent.reply(Msg('user', json.dumps({'context': {
        'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'
    }}), 'user'))).content)
    assert result['status'] == 'ok'
    assert len(calls) == 1
    assert calls[0][1]['data']['key'] == KEY
    app._display_results({'results': [{'agent_name': 'train_search', 'status': 'success', 'data': result}]})
    text = output.getvalue()
    for expected in ['G25', '二等座', '627.50', 'available', '聚合数据', 'https://www.juhe.cn/docs/api/id/817', '2026-10-09T09:00:00+08:00']:
        assert expected in text
    assert KEY not in text + caplog.text + json.dumps(result)

@pytest.mark.asyncio
@pytest.mark.parametrize('train', [True, False])
async def test_cli_paid_train_is_not_replayed_after_stage_write_failure(tmp_path, monkeypatch, caplog, train):
    from agents.lazy_agent_registry import LazyAgentRegistry
    from agents.orchestration_agent import OrchestrationAgent
    from context.memory_manager import MemoryManager

    monkeypatch.setattr(cli_module, 'RESILIENCE_CONFIG', {
        'max_retries': 1, 'retry_base_delay_sec': 0, 'retry_max_delay_sec': 0,
    })
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {'error_code': 0, 'result': [{
                'train_no': 'G25', 'departure_station': '北京南', 'arrival_station': '苏州北',
                'departure_time': '18:04', 'arrival_time': '22:32', 'enable_booking': 'Y',
                'prices': [{'seat_name': '二等座', 'seat_type_code': 'O', 'price': '627.50', 'num': '1'}]
            }]}
    def post(url, **kwargs):
        calls.append(url)
        return Response()
    provider = JuheTrainProvider(KEY, http_post=post, now_fn=lambda: NOW)
    memory = MemoryManager('offline-user', 'retry-test', storage_path=str(tmp_path))
    app = cli_module.TripEvidenceCLI()
    app.memory_manager = memory
    output = io.StringIO()
    app.console = Console(file=output, width=240, color_system=None)
    async def no_history(query):
        return ''
    app._get_long_term_summary = no_history
    fields = {'origin': '北京南', 'destination': '苏州北', 'departure_date': '2026-10-09'}
    collector_calls = []
    class Collector:
        async def reply(self, incoming):
            collector_calls.append(incoming)
            return Msg('collector', json.dumps(fields), 'assistant')
    schedule = [{'agent_name': 'event_collection', 'priority': 1}]
    if train:
        schedule.append({'agent_name': 'train_search', 'priority': 2})
    intent_calls = []
    class Intent:
        async def reply(self, incoming):
            intent_calls.append(incoming)
            if len(intent_calls) == 1:
                raise OSError('temporary model connection failure')
            return Msg('intent', json.dumps({'agent_schedule': schedule}), 'assistant')
    app.intention_agent = Intent()
    registry = {'event_collection': Collector()}
    if train:
        registry['train_search'] = LazyAgentRegistry(None, {}, providers={'train_search': provider})['train_search']
    app.orchestrator = OrchestrationAgent(agent_registry=registry, memory_manager=memory)
    original = memory.record_agent_stage
    stage_attempts = []
    def fail_once(name, priority, result):
        if name == ('train_search' if train else 'event_collection'):
            stage_attempts.append(result)
            if len(stage_attempts) == 1:
                raise OSError('temporary stage write failure')
        return original(name, priority, result)
    monkeypatch.setattr(memory, 'record_agent_stage', fail_once)
    if train:
        with pytest.raises(OSError, match='temporary stage write failure'):
            await app.process_query('北京南到苏州北')
        assert len(calls) == 1
        assert stage_attempts[0]['data']['status'] == 'ok'
        assert memory.session_store.read_runs()[-1]['status'] == 'error'
        assert not any(r.get('stage') == 'orchestration' for r in memory.session_store.read_runs())
    else:
        assert await app.process_query('普通行程需求') is True
        assert len(collector_calls) == 2
        assert len(stage_attempts) == 2
        assert calls == []
        assert memory.session_store.read_runs()[-1]['status'] == 'completed'
        assert any(r.get('stage') == 'orchestration' for r in memory.session_store.read_runs())
    assert len(intent_calls) == 2
    assert KEY not in output.getvalue() + caplog.text + json.dumps(memory.session_store.read_runs())
