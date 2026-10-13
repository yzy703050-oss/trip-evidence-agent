"""Legacy configuration entry point retained without framework initialization."""
from config import LLM_CONFIG

def init_agentscope(**kwargs):
    """Compatibility no-op for older callers."""
    return None


def get_model_config():
    """
    获取模型配置（用于Agent初始化）

    Returns:
        dict: 模型配置字典
    """
    return {
        "model_type": "openai_chat",  # 使用OpenAI兼容接口
        "config_name": "deepseek_api",
        "model_name": LLM_CONFIG["model_name"],
        "api_key": LLM_CONFIG["api_key"],
        "base_url": LLM_CONFIG["base_url"],
        "temperature": LLM_CONFIG.get("temperature", 0.7),
        "max_tokens": LLM_CONFIG.get("max_tokens", 2000),
    }
