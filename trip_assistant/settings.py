"""One configuration source; importing settings does not construct clients."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    llm_api_key: str = Field(default="", repr=False)
    deepseek_api_key: str = Field(default="", repr=False)
    llm_input_usd_per_1m_tokens: float | None = Field(default=None, ge=0)
    llm_output_usd_per_1m_tokens: float | None = Field(default=None, ge=0)
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    llm_max_tokens: int = Field(default=8192, gt=0)
    llm_thinking: Literal["disabled", "low", "medium", "high"] = "disabled"
    juhe_train_api_key: str = Field(default="", repr=False)
    amap_api_key: str = Field(default="", repr=False)
    rag_embedding_model: str = "data/models/bge-small-zh-v1.5"
    memory_context_budget_tokens: int = Field(default=20000, gt=0)
    planning_rounds: int = Field(default=8, gt=0)
    # Accept old disabled configurations, but never reactivate the retired experiment.
    single_pass_planning: bool = False
    tools_per_batch: int = Field(default=8, gt=0)
    external_queries_per_batch: int = Field(default=12, gt=0)
    external_concurrency: int = Field(default=3, gt=0)
    supplier_concurrency: int = Field(default=2, gt=0)
    trip_write_concurrency: int = Field(default=1, gt=0)
    turn_timeout: float = Field(default=600, gt=0)
    request_timeout: float = Field(default=30, gt=0)
    extra_retries: int = Field(default=2, ge=0)
    failure_window: float = Field(default=60, gt=0)
    failure_threshold: int = Field(default=5, gt=0)
    cooldown: float = Field(default=60, gt=0)
    half_open_concurrency: int = Field(default=1, gt=0)
    recovery_successes: int = Field(default=2, gt=0)
    evidence_max_age_seconds: float = Field(default=3600, gt=0)

    @field_validator("single_pass_planning")
    @classmethod
    def reject_retired_experiment(cls, value):
        if value:
            raise ValueError("单次生成实验已退役，使用当前Main→工具→Verifier流程。")
        return value


@lru_cache
def get_settings():
    return Settings()


def generation_options(model, *, temperature=0.3, max_tokens=8192, thinking=None):
    options = {"temperature": temperature, "max_tokens": max_tokens}
    if model.startswith("deepseek"):
        options["extra_body"] = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        if thinking:
            options["reasoning_effort"] = thinking
    return options
