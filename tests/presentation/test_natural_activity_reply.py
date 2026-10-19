from trip_assistant.presentation.facts import finalize


def selected():
    train = {
        "train_number": "G12",
        "seat_class": "二等座",
        "price_cny": "200",
        "departure_time": "08:00",
        "arrival_time": "10:00",
    }
    return [
        {
            "name": "write_trip",
            "operation": "select",
            "status": "completed",
            "trip_plan_id": "trip",
            "revision": 2,
            "validation": {
                "valid": True,
                "reconstructed_tasks": [
                    {
                        "train": train,
                        "hotel": {"hotel_name": "如家酒店"},
                        "schedule": {"check_in": "2026-10-17", "check_out": "2026-10-19"},
                        "content": [{"time": "15:00", "description": "散步", "strength": "soft"}],
                    }
                ],
            },
        }
    ]


def test_natural_reply_does_not_require_unknown_checklist_or_all_optional_fields():
    answer = (
        "明早乘G12去上海，08:00出发，10:00抵达，车票200元。住在如家酒店，下午15:00可以沿江散步。"
    )
    result = finalize(answer, selected())
    assert result["method"] == "main_agent" and not result["issues"]


def test_activity_suggestion_does_not_create_structured_booking_or_ticket_price():
    results = selected()
    checked = finalize("下午可以沿江散步。", results)
    assert checked["execution_facts"]["saved"] is True
    activity = results[0]["validation"]["reconstructed_tasks"][0]["content"][0]
    assert activity == {"time": "15:00", "description": "散步", "strength": "soft"}
    assert "booking" not in checked["execution_facts"]
