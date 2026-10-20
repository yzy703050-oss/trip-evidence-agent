
import pytest


URL = "https://www.example.xyz/guide?id=1&xsec_token=original#route"


def provider(fetch, **kwargs):
    from trip_assistant.providers.ddgs_guide import DDGSGuideProvider

    return DDGSGuideProvider(text_search=fetch, **kwargs)


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
