from datetime import datetime, timedelta, timezone

from trip_assistant.settings import Settings

KEY = "offline-cli-test-key"
NOW = datetime(2026, 10, 9, 9, tzinfo=timezone(timedelta(hours=8)))


def test_local_env_key_is_loaded_and_hidden_in_repr(tmp_path, monkeypatch):
    monkeypatch.delenv("JUHE_TRAIN_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("JUHE_TRAIN_API_KEY=" + KEY, encoding="utf-8")
    settings = Settings(_env_file=env)
    assert settings.juhe_train_api_key == KEY
    assert KEY not in repr(settings)
    assert Settings(_env_file=None).juhe_train_api_key == ""


def test_example_env_loads_with_optional_pricing_unset(monkeypatch):
    monkeypatch.delenv("LLM_INPUT_USD_PER_1M_TOKENS", raising=False)
    monkeypatch.delenv("LLM_OUTPUT_USD_PER_1M_TOKENS", raising=False)
    settings = Settings(_env_file=".env.example")
    assert settings.llm_input_usd_per_1m_tokens is None
    assert settings.llm_output_usd_per_1m_tokens is None
