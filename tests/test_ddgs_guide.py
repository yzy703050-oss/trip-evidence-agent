import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from agents.contracts import RunState
from travel_data.contracts import GuideQuery
from travel_data.tools import ToolExecutor

URL = 'https://www.example.xyz/guide?id=1&xsec_token=original#route'


def provider(fetch, **kwargs):
    from travel_data.ddgs_guide import DDGSGuideProvider
    return DDGSGuideProvider(text_search=fetch, **kwargs)


@pytest.mark.asyncio
async def test_first_valid_link_preserves_url_and_does_not_filter_platform():
    calls = []
    def search(query):
        calls.append(query)
        return [{'title': 'bad', 'href': 'javascript:alert(1)'},
                {'title': '北京旅游攻略', 'href': URL},
                {'title': 'second', 'href': 'https://example.com/second'}]
    result = await provider(search).search(GuideQuery('北京', []))
    assert calls == ['北京 旅游攻略']
    assert result.status == 'ok'
    assert len(result.items) == 1
    item = result.items[0]
    assert item['kind'] == 'link'
    assert '北京旅游攻略' in item['content']
    assert URL in item['content']
    assert item['source']['url'] == URL
    assert item['source']['provider'] == 'DDGS'
    assert result.source.url == URL
    assert result.query == {'destination': '北京', 'visit_dates': []}


@pytest.mark.asyncio
async def test_prefers_guide_over_unrelated_first_search_hit():
    result = await provider(lambda query: [
        {'title': '北京旅游大学', 'href': 'https://example.org/university'},
        {'title': '北京旅游攻略', 'href': URL},
    ]).search(GuideQuery('北京', []))
    assert len(result.items) == 1
    assert result.source.url == URL


@pytest.mark.asyncio
@pytest.mark.parametrize('rows', [[], [{'href': 'https://[broken'}], [{'href': 'file:///guide'}]])
async def test_empty_or_invalid_results_are_explicit(rows):
    result = await provider(lambda query: rows).search(GuideQuery('北京', []))
    assert result.status == 'ok'
    assert result.items == []
    assert '未找到' in result.message


@pytest.mark.asyncio
async def test_search_failure_does_not_expose_details():
    def fail(query):
        raise RuntimeError('secret-token')
    result = await provider(fail).search(GuideQuery('北京', []))
    assert result.status == 'error'
    assert result.items == []
    assert 'secret-token' not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_slow_search_is_bounded_and_does_not_block_event_loop():
    release = threading.Event()
    entered = threading.Event()
    def slow(query):
        entered.set()
        release.wait(2)
        return []
    try:
        result = await asyncio.wait_for(provider(slow, timeout=0.03).search(GuideQuery('北京', [])), 0.5)
        assert entered.is_set()
        assert result.status == 'error'
    finally:
        release.set()


def test_ddgs_client_uses_bounded_search(monkeypatch):
    import ddgs
    from travel_data.ddgs_guide import DDGSGuideProvider
    calls = []
    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [{'href': URL}]
    monkeypatch.setattr(ddgs, 'DDGS', Client)
    result = asyncio.run(DDGSGuideProvider().search(GuideQuery('北京', [])))
    assert result.status == 'ok'
    assert calls[0]['timeout'] <= 10
    assert calls[1][1]['max_results'] == 5
    assert calls[1][1]['region'] == 'cn-zh'
    assert calls[1][1]['backend'] == 'bing'


def test_cli_startup_wires_guide_without_api_key(monkeypatch):
    from test_juhe_cli_wiring import initialize
    from travel_data.ddgs_guide import DDGSGuideProvider
    app, output = initialize(monkeypatch, '')
    executor = app.harness.agent_registry['information_query'].tool_executor
    guide = executor.providers['travel_guide']
    assert isinstance(guide, DDGSGuideProvider)
    guide.text_search = lambda query: [{'title': '北京攻略', 'href': URL}]
    result = asyncio.run(executor.execute('travel_guide', {'destination': '北京'}, RunState('guide'), call_id='g'))
    from utils.response_renderer import finalize_business_result
    app._display_results(finalize_business_result({'domain_results': {'guide': result}}))
    assert URL in output.getvalue()


@pytest.mark.asyncio
async def test_guide_link_survives_info_agent_and_forwarded_answer():
    from agents.execution_harness import ExecutionHarness
    from test_information_agent_loop import info_class
    from test_main_harness import Main
    calls = []
    async def model(messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return SimpleNamespace(content=[{'type': 'tool_use', 'id': 'g', 'name': 'travel_guide',
                                            'input': {'destination': '北京'}}])
        return SimpleNamespace(text='{"summary":"已找到攻略链接。"}')
    main = Main([{'agent_name': 'information_query', 'priority': 2, 'requested_domains': ['guide']}])
    info = info_class()(model=model, tool_executor=ToolExecutor({'travel_guide': provider(
        lambda query: [{'title': '北京攻略', 'href': URL}])}))
    result = await ExecutionHarness(main_agent=main, agent_registry={'information_query': info}).run_turn(
        {'original_query': '给我北京旅游攻略链接'}, RunState('guide'))
    assert len(result['domain_results']['guide']['items']) == 1
    assert URL in result['final_answer']
    assert main.finalize_calls == 0
