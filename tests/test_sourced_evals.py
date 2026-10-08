from evals.v0_memory.runner import sourced_output_valid


def test_edd_rejects_unsourced_itinerary_prices():
    stages = [{"agent_name": "itinerary_planning", "content": {"data": {"itinerary": {"estimated_budget": "今日车票299元"}}}}]
    assert sourced_output_valid(stages) is False


def test_edd_accepts_guarded_suggestion():
    from travel_data.plan_guard import guard_itinerary
    stages = [{"agent_name": "itinerary_planning", "content": {"data": guard_itinerary({}, [])}}]
    assert sourced_output_valid(stages) is True


def test_edd_rejects_standalone_unsourced_offer():
    stages = [{"agent_name": "train_search", "content": {"data": {"status": "ok", "items": [{"id": "bad", "price_cny": "299"}]}}}]
    assert sourced_output_valid(stages) is False
