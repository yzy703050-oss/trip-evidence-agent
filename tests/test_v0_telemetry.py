"""Model and run telemetry records measured values without invented prices."""

from types import SimpleNamespace

import pytest

from config import Settings
from context.session_store import SessionStore
from context.telemetry import MeteredModel, model_stage


@pytest.mark.asyncio
async def test_metered_model_records_usage_latency_and_configured_cost(tmp_path):
    class Model:
        model_name = "test-model"

        async def __call__(self, _messages):
            return SimpleNamespace(
                content="ok", usage=SimpleNamespace(input_tokens=100, output_tokens=50)
            )

    store = SessionStore(tmp_path, "alice", "s1")
    model = MeteredModel(
        Model(), store.append_run, input_usd_per_million=2.0, output_usd_per_million=4.0
    )
    with model_stage("intent"):
        response = await model([{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    record = store.read_runs()[0]
    assert record["stage"] == "intent"
    assert record["model"] == "test-model"
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["estimated_cost_usd"] == pytest.approx(0.0004)
    assert record["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_missing_usage_or_rate_is_recorded_as_unknown_not_zero(tmp_path):
    class Model:
        async def __call__(self, _messages):
            return SimpleNamespace(content="ok", usage=None)

    store = SessionStore(tmp_path, "alice", "s1")
    await MeteredModel(Model(), store.append_run)([{"role": "user", "content": "hi"}])
    record = store.read_runs()[0]
    assert record["input_tokens"] is None
    assert record["output_tokens"] is None
    assert record["estimated_cost_usd"] is None


def test_optional_model_prices_load_from_local_dotenv(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LLM_INPUT_USD_PER_1M_TOKENS=0.27\nLLM_OUTPUT_USD_PER_1M_TOKENS=1.1\n", encoding="utf-8"
    )
    settings = Settings(_env_file=env_path)
    assert settings.llm_input_usd_per_1m_tokens == 0.27
    assert settings.llm_output_usd_per_1m_tokens == 1.1
