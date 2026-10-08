"""Memory query prompts must expose every stored profile field."""

import importlib.util
from pathlib import Path


def test_memory_query_formats_hotel_and_airline_preferences():
    script = (
        Path(__file__).resolve().parents[1]
        / ".claude"
        / "skills"
        / "memory-query"
        / "script"
        / "agent.py"
    )
    spec = importlib.util.spec_from_file_location("memory_query_preference_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    agent = module.MemoryQueryAgent(model=None)

    text = agent._format_preferences({"hotel_brands": "全季", "airlines": ["国航"]})
    assert "全季" in text
    assert "国航" in text
    assert "暂无偏好记录" not in text
