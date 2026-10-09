"""
Configuration for the Aligo Multi-Agent System
"""
import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings needed by the V0 command-line application."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    juhe_train_api_key: str = Field(default="", repr=False)

    llm_api_key: str = ""
    llm_model: str = "deepseek-flash"
    llm_base_url: str = "https://api.deepseek.com"
    llm_input_usd_per_1m_tokens: float | None = Field(default=None, ge=0)
    llm_output_usd_per_1m_tokens: float | None = Field(default=None, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()

# LLM Configuration
LLM_CONFIG = {
    "api_key": settings.llm_api_key or os.getenv("DEEPSEEK_API_KEY", ""),
    "model_name": settings.llm_model,
    "base_url": settings.llm_base_url,
    "input_usd_per_million": settings.llm_input_usd_per_1m_tokens,
    "output_usd_per_million": settings.llm_output_usd_per_1m_tokens,
    "temperature": 0.7,
    "max_tokens": 8192,
}

# System Configuration
SYSTEM_CONFIG = {
    "enable_llm": True,  # Set to True to use LLM (recommended), False for rule-based
    "log_level": "INFO",
    "max_retries": 3,
    "timeout": 60,  # Increased timeout for better stability
}

# RAG 知识库：嵌入模型（本地路径，无需连 HuggingFace）
RAG_CONFIG = {
    "embedding_model": "data/models/bge-small-zh-v1.5",
}

# 连接与可用性：重试、熔断、健康检查
RESILIENCE_CONFIG = {
    "max_retries": 3,              # 单次请求最大重试次数（与 SYSTEM_CONFIG 对齐）
    "retry_base_delay_sec": 1.0,   # 重试退避基数（秒）
    "retry_max_delay_sec": 30.0,   # 重试退避上限（秒）
    "circuit_failure_threshold": 5, # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后多少秒进入半开
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次后关闭
    "health_check_timeout_sec": 10.0,      # 健康检查请求超时（秒）
}
