from utils.skill_loader import SkillLoader


def test_planning_guide_describes_current_main_ability():
    guide = SkillLoader().get_skill_content('plan-trip')
    assert 'MainAgent' in guide
    assert 'IntentionAgent' not in guide and 'EventCollectionAgent' not in guide
    assert 'domain_results' in guide


def test_role_summary_can_exclude_private_skills():
    prompt = SkillLoader().get_skill_prompt(allowed_skills={'query-info'})
    assert 'query-info' in prompt
    assert 'train-search' not in prompt and 'ask-question' not in prompt
