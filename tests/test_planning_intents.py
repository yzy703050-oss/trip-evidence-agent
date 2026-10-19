import pytest


def test_model_cannot_derive_a_hotel_quote_requirement_from_requiring_a_hotel():
    from trip_assistant.trips.conditions import create_workflow

    value = {
        "confirmed_conditions": {},
        "tasks": [
            {
                "origin": "上海",
                "destination": "北京",
                "requires_hotel": True,
                "conditions": {"hotel_quote_required": True},
                "field_sources": {"hotel_quote_required": "derived"},
            }
        ],
    }
    with pytest.raises(ValueError, match="hotel quote requirement"):
        create_workflow(value, {"original_query": "只安排火车和酒店推荐"})
    value["tasks"][0]["field_sources"]["hotel_quote_required"] = "user"
    assert create_workflow(value, {})["tasks"][0]["conditions"]["hotel_quote_required"] is True
