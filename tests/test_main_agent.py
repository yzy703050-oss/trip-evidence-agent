"""Offline regression scenarios grouped by responsibility."""

from trip_assistant.providers.resources import SkillLoader


def test_role_summary_can_exclude_private_skills():
    prompt = SkillLoader().get_skill_prompt(allowed_skills={"query-info"})
    assert "query-info" in prompt
    assert "train-search" not in prompt and "ask-question" not in prompt
