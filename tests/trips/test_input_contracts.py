import pytest


def proposal():
    return {
        "operation": "propose",
        "tasks": [
            {
                "origin": "北京",
                "destination": "上海",
                "requires_hotel": True,
                "conditions": {"departure_date": "2026-10-20", "nights": 2},
                "field_sources": {"departure_date": "user", "nights": "proposal"},
                "condition_strengths": {"departure_date": "hard", "nights": "soft"},
                "items": [
                    {
                        "domain": "train",
                        "parameters": {},
                        "ranking": [{"field": "price_cny", "direction": "asc"}],
                    }
                ],
            }
        ],
    }


def test_tripplan_accepts_proposal_and_keeps_strength_separate_from_source():
    from trip_assistant.trips.contracts import TripPlanInput

    value = TripPlanInput.model_validate(proposal()).model_dump(exclude_none=True)
    assert value["tasks"][0]["condition_strengths"]["nights"] == "soft"
    assert value["tasks"][0]["field_sources"]["departure_date"] == "user"


@pytest.mark.parametrize(
    "field,value", [("status", "completed"), ("revision", 99), ("price_cny", 10)]
)
def test_model_cannot_author_execution_state_or_supplier_facts(field, value):
    from trip_assistant.trips.contracts import TripPlanInput

    data = proposal()
    data[field] = value
    with pytest.raises(ValueError):
        TripPlanInput.model_validate(data)


def test_item_rejects_unknown_sort_and_nonfinite_or_boolean_money():
    from trip_assistant.trips.contracts import TripPlanInput

    data = proposal()
    data["tasks"][0]["items"][0]["ranking"][0]["direction"] = "magic"
    with pytest.raises(ValueError):
        TripPlanInput.model_validate(data)
    for amount in [True, float("nan"), -1, "500"]:
        data = proposal()
        data["tasks"][0]["conditions"]["constraints"] = {"train_max_total_cny": amount}
        with pytest.raises(ValueError):
            TripPlanInput.model_validate(data)


@pytest.mark.parametrize(
    "item",
    [
        {
            "domain": "train",
            "parameters": {
                "origin": "上海",
                "destination": "北京",
                "departure_date": "2026-10-20",
                "candidate_offset": 5,
            },
        },
        {"domain": "hotel", "parameters": {"city": "北京", "refresh": True}},
        {"domain": "train", "constraints": {"train_max_total_cny": "500"}},
        {"domain": "train", "constraints": {"made_up": True}},
    ],
)
def test_query_item_rejects_unsupported_parameters_and_untyped_money(item):
    from trip_assistant.trips.contracts import QueryItem

    with pytest.raises(ValueError):
        QueryItem.model_validate(item)
