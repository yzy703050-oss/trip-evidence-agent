"""Offline regression scenarios grouped by responsibility."""

from trip_assistant.providers.resources import SkillLoader


def test_planning_guide_describes_current_main_ability():
    guide = SkillLoader().get_skill_content("plan-trip")
    assert "MainAgent" in guide
    assert "IntentionAgent" not in guide and "EventCollectionAgent" not in guide
    assert "write_trip" in guide and "query_id/result_revision/candidate_id" in guide


def test_role_summary_can_exclude_private_skills():
    prompt = SkillLoader().get_skill_prompt(allowed_skills={"query-info"})
    assert "query-info" in prompt
    assert "train-search" not in prompt and "ask-question" not in prompt
