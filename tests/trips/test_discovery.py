def test_summary_filters_without_loading_plans_and_session_is_only_hint():
    from trip_assistant.trips.discovery import discover

    summaries = [
        {
            "id": "a",
            "updated_at": "2026-10-10",
            "tasks": [
                {
                    "destination": "上海",
                    "purpose": "business",
                    "conditions": {"departure_date": "2026-10-20"},
                }
            ],
        },
        {
            "id": "b",
            "updated_at": "2026-10-14",
            "tasks": [
                {
                    "destination": "上海",
                    "purpose": "visit",
                    "conditions": {"departure_date": "2026-10-22"},
                }
            ],
        },
    ]
    assert [
        x["id"]
        for x in discover(
            summaries, {"city": "上海", "purpose": "business", "date": "2026-10-20"}, "s"
        )
    ] == ["a"]
    assert len(discover(summaries, {"city": "上海"}, "s")) == 2
