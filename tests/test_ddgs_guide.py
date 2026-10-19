import asyncio
import json
import threading

import pytest

from trip_assistant.providers.contracts import GuideQuery

URL = "https://www.example.xyz/guide?id=1&xsec_token=original#route"


def provider(fetch, **kwargs):
    from trip_assistant.providers.ddgs_guide import DDGSGuideProvider

    return DDGSGuideProvider(text_search=fetch, **kwargs)


@pytest.mark.asyncio
async def test_first_valid_link_preserves_url_and_does_not_filter_platform():
    calls = []

    def search(query):
        calls.append(query)
        return [
            {"title": "bad", "href": "javascript:alert(1)"},
            {"title": "北京旅游攻略", "href": URL},
            {"title": "second", "href": "https://example.com/second"},
        ]

    result = await provider(search).search(GuideQuery("北京", []))
    assert calls == ["北京 旅游攻略"]
    assert result.status == "ok"
    assert len(result.items) == 1
    item = result.items[0]
    assert item["kind"] == "link"
    assert "北京旅游攻略" in item["content"]
    assert URL in item["content"]
    assert item["source"]["url"] == URL
    assert item["source"]["provider"] == "DDGS"
    assert result.source.url == URL
    assert result.query == {"destination": "北京", "visit_dates": []}


@pytest.mark.asyncio
async def test_prefers_guide_over_unrelated_first_search_hit():
    result = await provider(
        lambda query: [
            {"title": "北京旅游大学", "href": "https://example.org/university"},
            {"title": "北京旅游攻略", "href": URL},
        ]
    ).search(GuideQuery("北京", []))
    assert len(result.items) == 1
    assert result.source.url == URL


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [[], [{"href": "https://[broken"}], [{"href": "file:///guide"}]])
async def test_empty_or_invalid_results_are_explicit(rows):
    result = await provider(lambda query: rows).search(GuideQuery("北京", []))
    assert result.status == "ok"
    assert result.items == []
    assert "未找到" in result.message


@pytest.mark.asyncio
async def test_search_failure_does_not_expose_details():
    def fail(query):
        raise RuntimeError("secret-token")

    result = await provider(fail).search(GuideQuery("北京", []))
    assert result.status == "error"
    assert result.items == []
    assert "secret-token" not in json.dumps(result.to_dict())


@pytest.mark.asyncio
async def test_slow_search_is_bounded_and_does_not_block_event_loop():
    release = threading.Event()
    entered = threading.Event()

    def slow(query):
        entered.set()
        release.wait(2)
        return []

    try:
        result = await asyncio.wait_for(
            provider(slow, timeout=0.03).search(GuideQuery("北京", [])), 0.5
        )
        assert entered.is_set()
        assert result.status == "error"
    finally:
        release.set()


def test_ddgs_client_uses_bounded_search(monkeypatch):
    import ddgs

    from trip_assistant.providers.ddgs_guide import DDGSGuideProvider

    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def text(self, query, **kwargs):
            calls.append((query, kwargs))
            return [{"href": URL}]

    monkeypatch.setattr(ddgs, "DDGS", Client)
    result = asyncio.run(DDGSGuideProvider().search(GuideQuery("北京", [])))
    assert result.status == "ok"
    assert calls[0]["timeout"] <= 10
    assert calls[1][1]["max_results"] == 5
    assert calls[1][1]["region"] == "cn-zh"
    assert calls[1][1]["backend"] == "bing"


def test_cli_startup_keeps_guide_disabled_without_loading_its_provider(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    app = create_application(
        Settings(_env_file=None, juhe_train_api_key="", amap_api_key=""),
        model=ScriptedModel([]),
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    assert "guide" not in app.providers
    assert not app.raw_model.messages


@pytest.mark.asyncio
async def test_native_guide_query_stays_disabled_even_with_legacy_provider_injected(tmp_path):
    from types import SimpleNamespace

    from support.native import NativeRuntimeFixture

    calls = []

    def fetch(query):
        calls.append(query)
        return [{"title": "北京攻略", "href": URL}]

    runtime = NativeRuntimeFixture(tmp_path, {"guide": provider(fetch)})
    args = {
        "request_kind": "lookup",
        "query_plan": [{"domain": "guide", "parameters": {"destination": "北京"}}],
    }

    runtime.model.script = [
        SimpleNamespace(
            content=[{"type": "tool_use", "id": "guide", "name": "query", "input": args}]
        ),
        "目前没有攻略查询工具，可根据你的需求提出游玩建议。",
    ]
    result = await runtime.run("给我北京旅游攻略链接")
    assert len(runtime.model.messages) == 2
    assert result["tool_results"][0]["status"] == "rejected"
    assert URL not in result["final_answer"] and calls == []
