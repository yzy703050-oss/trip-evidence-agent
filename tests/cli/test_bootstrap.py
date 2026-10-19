import pytest

from trip_assistant.settings import Settings


@pytest.mark.asyncio
async def test_application_injected_preferences_use_main_once_and_no_eager_rag(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application

    model = ScriptedModel(["没有偏好"])
    app = create_application(
        Settings(_env_file=None),
        model=model,
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="s",
    )
    result = await app.run("查看偏好")
    assert len(model.messages) == 1 and result["final_answer"] == "没有偏好"
    assert app.policy_retriever.loaded is False


@pytest.mark.asyncio
async def test_new_session_does_not_inherit_old_trip_binding(tmp_path):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application

    settings = Settings(_env_file=None)
    old = create_application(
        settings,
        model=ScriptedModel(["你好"]),
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="old",
    )
    await old.run("旧对话")
    new = create_application(
        settings,
        model=ScriptedModel(["你好"]),
        providers={},
        storage_root=tmp_path,
        user_id="a",
        session_id="new",
    )
    await new.run("新对话")
    assert "旧对话" not in str(new.raw_model.messages) and new.sessions.session_id == "new"


@pytest.mark.parametrize("thinking", ["disabled", "low", "medium", "high"])
def test_bootstrap_forwards_generation_settings_to_actual_model_factory(
    tmp_path, monkeypatch, thinking
):
    from support.native import ScriptedModel

    from trip_assistant.bootstrap import create_application
    from trip_assistant.settings import Settings

    captured = {}

    def factory(name, **kwargs):
        captured.update(name=name, **kwargs)
        return ScriptedModel([])

    monkeypatch.setattr("trip_assistant.bootstrap.OpenAIChatModel", factory)
    settings = Settings(
        _env_file=None,
        llm_model="deepseek-test",
        llm_api_key="offline-only",
        llm_base_url="https://example.com/v1",
        llm_max_tokens=123,
        llm_thinking=thinking,
    )
    app = create_application(
        settings, providers={}, storage_root=tmp_path, user_id="a", session_id="s"
    )
    assert captured["name"] == "deepseek-test" and captured["client_kwargs"]["timeout"] == 30
    assert captured["client_kwargs"]["base_url"] == "https://example.com/v1"
    expected = {
        "temperature": 0.3,
        "max_tokens": 123,
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert captured["generate_kwargs"] == expected
    assert not app.raw_model.messages
