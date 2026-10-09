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
    monkeypatch.setattr(cli_module.AligoCLI, 'choose_session_id', lambda self: 'offline-session')
    app = cli_module.AligoCLI()
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
